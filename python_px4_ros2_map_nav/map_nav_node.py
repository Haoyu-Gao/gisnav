"""Module that contains the MapNavNode ROS 2 node."""
import rclpy
import os
import traceback
import yaml
import math
import cProfile
import io
import pstats
import numpy as np
import cv2
import time
import shapely

# Import and configure torch for multiprocessing
import torch
try:
    torch.multiprocessing.set_start_method('spawn', force=True)
except RuntimeError:
    pass
torch.set_num_threads(1)

from multiprocessing.pool import Pool, AsyncResult  # Used for WMS client process, not for torch
from pyproj import Geod
from typing import Optional, Union, Tuple, get_args, List
from rclpy.node import Node
from rcl_interfaces.msg import ParameterDescriptor
from owslib.wms import WebMapService
from geojson import Point, Polygon, Feature, FeatureCollection, dump

from cv_bridge import CvBridge
from scipy.spatial.transform import Rotation
from functools import partial, lru_cache
from python_px4_ros2_map_nav.util import setup_sys_path, pix_to_wgs84, BBox, Dim, get_bbox_center, \
    rotate_and_crop_map, visualize_homography, get_fov_and_c, LatLon, fov_center, get_angle, rotate_point, TimePair, \
    create_src_corners, RPY, LatLonAlt, ImageFrame, assert_type, assert_ndim, assert_len, assert_shape, MapFrame, \
    pix_to_wgs84_affine
from python_px4_ros2_map_nav.ros_param_defaults import Defaults
from px4_msgs.msg import VehicleVisualOdometry, VehicleAttitude, VehicleLocalPosition, VehicleGlobalPosition, \
    GimbalDeviceAttitudeStatus, GimbalDeviceSetAttitude
from sensor_msgs.msg import CameraInfo, Image

# Add the share folder to Python path
share_dir, superglue_dir = setup_sys_path()

# Import this after util.setup_sys_path has been called
from python_px4_ros2_map_nav.superglue import SuperGlue


@lru_cache(maxsize=1)
def _cached_wms_client(url: str, version_: str, timeout_: int) -> WebMapService:
    """Returns a cached WMS client.

    The WMS requests are intended to be handled in a dedicated process (to avoid blocking the main thread), so this
    function is lru_cache'd to avoid recurrent instantiations every time a WMS request is sent. For example usage, see
    :meth:`python_px4_ros2_map_nav.MapNavNode._wms_pool_worker` method.

    :param url: WMS server endpoint url
    :param version_: WMS server version
    :param timeout_: WMS request timeout seconds
    :return: The cached WMS client
    """
    assert_type(str, url)
    assert_type(str, version_)
    assert_type(int, timeout_)
    try:
        return WebMapService(url, version=version_, timeout=timeout_)
    except Exception as e:
        raise e  # TODO: handle gracefully (e.g. ConnectionRefusedError)


class MapNavNode(Node):
    """ROS 2 Node that publishes position estimate based on visual match of drone video to map of same location."""
    # scipy Rotations: {‘X’, ‘Y’, ‘Z’} for intrinsic, {‘x’, ‘y’, ‘z’} for extrinsic rotations
    EULER_SEQUENCE = 'YXZ'
    EULER_SEQUENCE_VEHICLE = 'xyz'  # TODO: remove or replace
    EULER_SEQUENCE_VISUAL = 'xyz'  # TODO: remove or replace

    # Minimum matches for homography estimation, should be at least 4
    HOMOGRAPHY_MINIMUM_MATCHES = 4

    # Encoding of input video (input to CvBridge)
    IMAGE_ENCODING = 'bgr8'  # E.g. gscam2 only supports bgr8 so this is used to override encoding in image header

    # Local frame reference for px4_msgs.msg.VehicleVisualOdometry messages
    LOCAL_FRAME_NED = 0

    # Ellipsoid model used by pyproj
    PYPROJ_ELLIPSOID = 'WGS84'

    # Default name of config file
    CONFIG_FILE_DEFAULT = "params.yml"

    # Minimum and maximum publish frequencies for EKF2 fusion
    MINIMUM_PUBLISH_FREQUENCY = 30
    MAXIMUM_PUBLISH_FREQUENCY = 50

    # Logs a warning if publish frequency is close to the bounds of desired publish frequency
    VVO_PUBLISH_FREQUENCY_WARNING_PADDING = 3

    # For logging a warning if fx and fy differ too much (assume they are the same)
    FOCAL_LENGTH_DIFF_THRESHOLD = 0.05

    # ROS 2 QoS profiles for topics
    # TODO: add duration to match publishing frequency, and publish every time (even if NaN)s.
    # If publishign for some reason stops, it can be assumed that something has gone very wrong
    PUBLISH_QOS_PROFILE = rclpy.qos.QoSProfile(history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                                               reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                                               depth=1)

    # Padding for EKF2 timestamp to optionally ensure published VVO message has a later timstamp than EKF2 system
    EKF2_TIMESTAMP_PADDING = 500000  # microseconds

    # Maps properties to microRTPS bridge topics and message definitions
    # TODO: get rid of static TOPICS and dynamic _topics dictionaries - just use one dictionary, initialize it in constructor?
    TOPIC_NAME_KEY = 'topic_name'
    CLASS_KEY = 'class'
    SUBSCRIBE_KEY = 'subscribe'  # Used as key in both Matcher.TOPICS and Matcher._topics
    PUBLISH_KEY = 'publish'  # Used as key in both Matcher.TOPICS and Matcher._topics
    VEHICLE_VISUAL_ODOMETRY_TOPIC_NAME = 'VehicleVisualOdometry_PubSubTopic'  # TODO: Used when publishing, do this in some bette way
    TOPICS = [
        {
            TOPIC_NAME_KEY: 'VehicleLocalPosition_PubSubTopic',
            CLASS_KEY: VehicleLocalPosition,
            SUBSCRIBE_KEY: True
        },
        {
            TOPIC_NAME_KEY: 'VehicleGlobalPosition_PubSubTopic',
            CLASS_KEY: VehicleGlobalPosition,
            SUBSCRIBE_KEY: True
        },
        {
            TOPIC_NAME_KEY: 'VehicleAttitude_PubSubTopic',
            CLASS_KEY: VehicleAttitude,
            SUBSCRIBE_KEY: True
        },
        {
            TOPIC_NAME_KEY: 'GimbalDeviceAttitudeStatus_PubSubTopic',
            CLASS_KEY: GimbalDeviceAttitudeStatus,
            SUBSCRIBE_KEY: True
        },
        {
            TOPIC_NAME_KEY: 'GimbalDeviceSetAttitude_PubSubTopic',
            CLASS_KEY: GimbalDeviceSetAttitude,
            SUBSCRIBE_KEY: True
        },
        {
            TOPIC_NAME_KEY: 'camera_info',
            CLASS_KEY: CameraInfo,
            SUBSCRIBE_KEY: True
        },
        {
            TOPIC_NAME_KEY: 'image_raw',
            CLASS_KEY: Image,
            SUBSCRIBE_KEY: True
        },
        {
            TOPIC_NAME_KEY: VEHICLE_VISUAL_ODOMETRY_TOPIC_NAME,
            CLASS_KEY: VehicleVisualOdometry,
            PUBLISH_KEY: True
        }
    ]

    def __init__(self, node_name: str, share_directory: str, superglue_directory: str,
                 config: str = CONFIG_FILE_DEFAULT) -> None:
        """Initializes the ROS 2 node.

        :param node_name: Name of the node
        :param share_directory: Path of the share directory with configuration and other files
        :param superglue_directory: Path of the directory with SuperGlue related files
        :param config: Path to the config file in the share folder
        """
        assert_type(str, node_name)
        super().__init__(node_name)
        self.name = node_name
        assert_type(str, share_directory)
        assert_type(str, superglue_directory)
        assert_type(str, config)
        self._share_dir = share_directory
        self._superglue_dir = superglue_directory

        # Setup config and declare ROS parameters
        self._config = self._load_config(config)
        params = self._config.get(node_name, {}).get('ros__parameters')
        assert_type(dict, params)
        self._declare_ros_params(params)

        # WMS client and requests in a separate process
        self._wms_results = None  # Must check for None when using this
        self._wms_pool = Pool(1)  # Do not increase the process count, it should be 1

        # Setup map update timer
        self._map_update_timer = self._setup_map_update_timer()

        # Dict for storing all microRTPS bridge subscribers and publishers
        self._topics = {self.PUBLISH_KEY: {}, self.SUBSCRIBE_KEY: {}}
        self._setup_topics()

        # Setup vehicle visual odometry publisher timer
        self._publish_timer = self._setup_publish_timer()
        self._publish_timestamp = None

        # Converts image_raw to cv2 compatible image
        self._cv_bridge = CvBridge()

        # Setup SuperGlue
        self._stored_inputs = None  # Must check for None when using this
        self._superglue_results = None  # Must check for None when using this
        # Do not increase the process count, it should be 1
        self._superglue_pool = torch.multiprocessing.Pool(1, initializer=self._superglue_init_worker,
                                                          initargs=(self._config.get(self.name, {}), ))

        # Used for pyproj transformations
        self._geod = Geod(ellps=self.PYPROJ_ELLIPSOID)

        # Must check for None when using these
        # self._image_frame = None  # Not currently used / needed
        self._map_frame = None
        self._previous_map_frame = None

        self._local_origin = None  # Estimated EKF2 local frame origin WGS84 coordinates

        self._time_sync = None  # For storing local and foreign (EKF2) timestamps

        self._pose_covariance_data_window = None  # Windowed observations for computing pose cross-covariance matrix

        # Properties that are mapped to microRTPS bridge topics, must check for None when using them
        self._camera_info = None
        self._vehicle_local_position = None
        self._vehicle_global_position = None
        self._vehicle_attitude = None
        self._gimbal_device_attitude_status = None
        self._gimbal_device_set_attitude = None
        self._vehicle_visual_odometry = None  # To be published by _timer_callback (see _timer property)

    @property
    def name(self) -> dict:
        """Node name."""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        assert_type(str, value)
        self._name = value

    @property
    def _config(self) -> dict:
        """ROS parameters and other configuration info."""
        return self.__config

    @_config.setter
    def _config(self, value: dict) -> None:
        assert_type(dict, value)
        self.__config = value

    @property
    def _local_origin(self) -> Optional[LatLonAlt]:
        """Estimate of EKF2 local frame origin WGS84 coordinates.

        This property is needed when :class:`px4_msgs.msg.VehicleGlobalPosition` nor
        :class:`px4_msgs.msg.VehicleLocalPosition` contain global position reference information. The value is then
        estimated from the current visual global position estimate and local position coordinates.
        """
        return self.__local_origin

    @_local_origin.setter
    def _local_origin(self, value: Optional[LatLonAlt]) -> None:
        assert_type(get_args(Optional[LatLonAlt]), value)
        self.__local_origin = value

    @property
    def _time_sync(self) -> Optional[TimePair]:
        """A :class:`python_px4_ros2_map_nav.util.TimePair` with local and foreign (EKF2) timestamps in microseconds

        The pair will contain the local system time and the EKF2 time received via the PX4-ROS 2 bridge. The pair can
        then at any time be used to locally estimate the EKF2 system time.
        """
        return self.__time_sync

    @_time_sync.setter
    def _time_sync(self, value: Optional[TimePair]) -> None:
        assert_type(get_args(Optional[TimePair]), value)
        self.__time_sync = value

    @property
    def _pose_covariance_data_window(self) -> Optional[np.ndarray]:
        """Windowed data for computing cross-covariance matrix of pose variables for :class:`VehicleVisualOdometry`
        messages
        """
        return self.__pose_covariance_data_window

    @_pose_covariance_data_window.setter
    def _pose_covariance_data_window(self, value: Optional[np.ndarray]) -> None:
        assert_type(get_args(Optional[np.ndarray]), value)
        self.__pose_covariance_data_window = value

    @property
    def _wms_pool(self) -> Pool:
        """Web Map Service client for fetching map rasters."""
        return self.__wms_pool

    @_wms_pool.setter
    def _wms_pool(self, value: Pool) -> None:
        assert_type(Pool, value)
        self.__wms_pool = value

    @property
    def _wms_results(self) -> Optional[AsyncResult]:
        """Asynchronous results from a WMS client request."""
        return self.__wms_results

    @_wms_results.setter
    def _wms_results(self, value: Optional[AsyncResult]) -> None:
        assert_type(get_args(Optional[AsyncResult]), value)
        self.__wms_results = value

    @property
    def _map_update_timer(self) -> rclpy.timer.Timer:
        """Timer for throttling map update WMS requests."""
        return self.__map_update_timer

    @_map_update_timer.setter
    def _map_update_timer(self, value: rclpy.timer.Timer) -> None:
        assert_type(rclpy.timer.Timer, value)
        self.__map_update_timer = value

    @property
    def _superglue_pool(self) -> torch.multiprocessing.Pool:
        """Pool for running SuperGlue in dedicated process."""
        return self.__superglue_pool

    @_superglue_pool.setter
    def _superglue_pool(self, value: torch.multiprocessing.Pool) -> None:
        # TODO assert type
        #assert_type(torch.multiprocessing.Pool, value)
        self.__superglue_pool = value

    @property
    def _stored_inputs(self) -> dict:
        """Inputs stored at time of launching a new asynchronous match that are needed for processing its results.

        See :meth:`~_process_matches` for description of keys and values stored in the dictionary.
        """
        return self.__stored_inputs

    @_stored_inputs.setter
    def _stored_inputs(self, value: Optional[dict]) -> None:
        assert_type(get_args(Optional[dict]), value)
        self.__stored_inputs = value

    @property
    def _superglue_results(self) -> Optional[AsyncResult]:
        """Asynchronous results from a SuperGlue process."""
        return self.__superglue_results

    @_superglue_results.setter
    def _superglue_results(self, value: Optional[AsyncResult]) -> None:
        assert_type(get_args(Optional[AsyncResult]), value)
        self.__superglue_results = value

    @property
    def _superglue(self) -> SuperGlue:
        """SuperGlue graph neural network (GNN) estimator for matching keypoints between images."""
        return self.__superglue

    @_superglue.setter
    def _superglue(self, value: SuperGlue) -> None:
        assert_type(SuperGlue, value)
        self.__superglue = value

    @property
    def _publish_timer(self) -> rclpy.timer.Timer:
        """Timer for controlling publish frequency of outgoing VehicleVisualOdometry messages."""
        return self.__timer

    @_publish_timer.setter
    def _publish_timer(self, value: rclpy.timer.Timer) -> None:
        assert_type(rclpy.timer.Timer, value)
        self.__timer = value

    @property
    def _publish_timestamp(self) -> Optional[int]:
        """Timestamp in of when last VehicleVisualOdometry message was published."""
        return self.__publish_timestamp

    @_publish_timestamp.setter
    def _publish_timestamp(self, value: Optional[int]) -> None:
        assert_type(get_args(Optional[int]), value)
        self.__publish_timestamp = value

    @property
    def _topics(self) -> dict:
        """Dictionary that stores all rclpy publishers and subscribers."""
        return self.__topics

    @_topics.setter
    def _topics(self, value: dict) -> None:
        assert_type(dict, value)
        self.__topics = value

    @property
    def _vehicle_visual_odometry(self) -> Optional[VehicleVisualOdometry]:
        """Outgoing VehicleVisualOdometry message waiting to be published."""
        return self.__vehicle_visual_odometry

    @_vehicle_visual_odometry.setter
    def _vehicle_visual_odometry(self, value: Optional[VehicleVisualOdometry]) -> None:
        assert_type(get_args(Optional[VehicleVisualOdometry]), value)
        self.__vehicle_visual_odometry = value

    @property
    def _geod(self) -> Geod:
        """Stored pyproj Geod instance for performing geodetic computations."""
        return self.__geod

    @_geod.setter
    def _geod(self, value: Geod) -> None:
        assert_type(Geod, value)
        self.__geod = value

    @property
    def _share_dir(self) -> str:
        """Path to share directory"""
        return self.__share_dir

    @_share_dir.setter
    def _share_dir(self, value: str) -> None:
        assert_type(str, value)
        self.__share_dir = value

    @property
    def _superglue_dir(self) -> str:
        """Path to SuperGlue directory."""
        return self.__superglue_dir

    @_superglue_dir.setter
    def _superglue_dir(self, value: str) -> None:
        assert_type(str, value)
        self.__superglue_dir = value

    @property
    def _map_frame(self) -> Optional[MapFrame]:
        """The map raster from the WMS server response along with supporting metadata."""
        return self.__map_frame

    @_map_frame.setter
    def _map_frame(self, value: Optional[MapFrame]) -> None:
        assert_type(get_args(Optional[MapFrame]), value)
        self.__map_frame = value

    @property
    def _cv_bridge(self) -> CvBridge:
        """CvBridge that decodes incoming PX4-ROS 2 bridge images to cv2 images."""
        return self.__cv_bridge

    @_cv_bridge.setter
    def _cv_bridge(self, value: CvBridge) -> None:
        assert_type(CvBridge, value)
        self.__cv_bridge = value

    @property
    def _previous_map_frame(self) -> Optional[MapFrame]:
        """The previous map frame which is compared to current map frame to determine need for another update."""
        return self.__previous_map_frame

    @_previous_map_frame.setter
    def _previous_map_frame(self, value: Optional[MapFrame]) -> None:
        assert_type(get_args(Optional[MapFrame]), value)
        self.__previous_map_frame = value

    @property
    def _camera_info(self) -> Optional[CameraInfo]:
        """CameraInfo received via the PX4-ROS 2 bridge."""
        return self.__camera_info

    @_camera_info.setter
    def _camera_info(self, value: Optional[CameraInfo]) -> None:
        assert_type(get_args(Optional[CameraInfo]), value)
        self.__camera_info = value

    @property
    def _vehicle_local_position(self) -> Optional[VehicleLocalPosition]:
        """VehicleLocalPosition received via the PX4-ROS 2 bridge."""
        return self.__vehicle_local_position

    @_vehicle_local_position.setter
    def _vehicle_local_position(self, value: Optional[VehicleLocalPosition]) -> None:
        assert_type(get_args(Optional[VehicleLocalPosition]), value)
        self.__vehicle_local_position = value

    @property
    def _vehicle_global_position(self) -> Optional[VehicleGlobalPosition]:
        """VehicleGlobalPosition received via the PX4-ROS 2 bridge."""
        return self.__vehicle_global_position

    @_vehicle_global_position.setter
    def _vehicle_global_position(self, value: Optional[VehicleGlobalPosition]) -> None:
        assert_type(get_args(Optional[VehicleGlobalPosition]), value)
        self.__vehicle_global_position = value

    @property
    def _vehicle_attitude(self) -> Optional[VehicleAttitude]:
        """VehicleAttitude received via the PX4-ROS 2 bridge."""
        return self.__vehicle_attitude

    @_vehicle_attitude.setter
    def _vehicle_attitude(self, value: Optional[VehicleAttitude]) -> None:
        assert_type(get_args(Optional[VehicleAttitude]), value)
        self.__vehicle_attitude = value

    @property
    def _gimbal_device_attitude_status(self) -> Optional[GimbalDeviceAttitudeStatus]:
        """GimbalDeviceAttitudeStatus received via the PX4-ROS 2 bridge."""
        return self.__gimbal_device_attitude_status

    @_gimbal_device_attitude_status.setter
    def _gimbal_device_attitude_status(self, value: Optional[GimbalDeviceAttitudeStatus]) -> None:
        assert_type(get_args(Optional[GimbalDeviceAttitudeStatus]), value)
        self.__gimbal_device_attitude_status = value

    @property
    def _gimbal_device_set_attitude(self) -> Optional[GimbalDeviceSetAttitude]:
        """GimbalDeviceSetAttitude received via the PX4-ROS 2 bridge."""
        return self.__gimbal_device_set_attitude

    @_gimbal_device_set_attitude.setter
    def _gimbal_device_set_attitude(self, value: Optional[GimbalDeviceSetAttitude]) -> None:
        assert_type(get_args(Optional[GimbalDeviceSetAttitude]), value)
        self.__gimbal_device_set_attitude = value

    def _covariance_window_full(self) -> bool:
        """Returns true if the covariance estimation window is full and a covariance matrix can be estimated

        :return: True if :py:attr:`~_pose_covariance_data_window` is full
        """
        window_length = self.get_parameter('misc.covariance_estimation_length').get_parameter_value().integer_value
        obs_count = len(self._pose_covariance_data_window)
        if self._pose_covariance_data_window is not None and obs_count == window_length:
            return True
        else:
            assert 0 <= obs_count < window_length
            return False

    def _push_covariance_data(self, position: tuple, rotation: tuple) -> None:
        """Pushes position and rotation observations to :py:attr:`~_pose_covariance_data_window`

        Pops the oldest observation from the window if needed.

        :param position: Pose translation (x, y, z) from local frame origin
        :param rotation: Rotations in radians about x, y and z axes, respectively
        :return:
        """
        if self._pose_covariance_data_window is None:
            # Compute rotations in radians around x, y, z axes (get RPY and convert to radians?)
            self._pose_covariance_data_window = np.array(position + rotation).reshape(-1, 6)
        else:
            window_length = self.get_parameter('misc.covariance_estimation_length').get_parameter_value().integer_value
            assert window_length > 0, f'Window length for estimating cross-covariances should be >0 ({window_length} ' \
                                      f'provided).'
            obs_count = len(self._pose_covariance_data_window)
            assert 0 <= obs_count <= window_length
            if obs_count == window_length:
                # Pop oldest observation
                self._pose_covariance_data_window = np.delete(self._pose_covariance_data_window, 0, 0)

            # Add newest observation
            self._pose_covariance_data_window = np.vstack((self._pose_covariance_data_window, position + rotation))

    def _setup_publish_timer(self) -> rclpy.timer.Timer:
        """Sets up a timer to control the publish rate of vehicle visual odometry.

        At regular intervals, this timer is intended to publish the VehicleVisualOdometry message stored at
        :py:attr:`~_vehicle_visual_odometry`. The message is generated by :meth:`~_create_vehicle_visual_odometry_msg`.
        The timer is needed so that the publishing frequency can be set to whatever is required for EKF2 fusion.

        :return: The timer instance
        """
        frequency = self.get_parameter('misc.publish_frequency').get_parameter_value().integer_value
        assert_type(int, frequency)
        if not 0 <= frequency:
            error_msg = f'Publish frequency must be >0 Hz ({frequency} provided).'
            self.get_logger().error(error_msg)
            raise ValueError(error_msg)
        if not self.MINIMUM_PUBLISH_FREQUENCY <= frequency <= self.MAXIMUM_PUBLISH_FREQUENCY:
            warn_msg = f'Publish frequency should be between {self.MINIMUM_PUBLISH_FREQUENCY} and ' \
                       f'{self.MAXIMUM_PUBLISH_FREQUENCY} Hz ({frequency} provided) for EKF2 filter.'
            self.get_logger().warn(warn_msg)
        timer_period = 1.0 / frequency
        self.get_logger().debug(f'Setting up publish timer with period {timer_period} / frequency {frequency} Hz.')
        timer = self.create_timer(timer_period, self._vehicle_visual_odometry_timer_callback)
        return timer

    def _setup_map_update_timer(self) -> rclpy.timer.Timer:
        """Sets up a timer to throttle map update requests.

        Initially map updates were triggered in VehicleGlobalPosition message callbacks, but were moved to a separate
        timer since map updates may be needed even if the EKF2 filter does not publish a global position reference (e.g.
        when GPS fusion is turned off in the EKF2_AID_MASK).

        :return: The timer instance
        """
        timer_period = self.get_parameter('map_update.update_delay').get_parameter_value().integer_value
        assert_type(int, timer_period)
        if not 0 <= timer_period:
            error_msg = f'Map update delay must be >0 seconds ({timer_period} provided).'
            self.get_logger().error(error_msg)
            raise ValueError(error_msg)
        timer = self.create_timer(timer_period, self._map_update_timer_callback)
        return timer

    def _latlonalt_from_vehicle_global_position(self) -> LatLonAlt:
        """Returns lat, lon in WGS84 coordinates and alt in meters from VehicleGlobalPosition.

        The individual values of the LatLonAlt tuple may be None if vehicle global position is not available but a
        LatLonAlt tuple is returned nevertheless.

        :return: LatLonAlt tuple"""
        lat, lon, alt = None, None, None
        if self._vehicle_global_position is not None:
            assert hasattr(self._vehicle_global_position, 'lat') and hasattr(self._vehicle_global_position, 'lon') and \
                   hasattr(self._vehicle_global_position, 'alt')
            lat, lon, alt = self._vehicle_global_position.lat, self._vehicle_global_position.lon, \
                            self._vehicle_global_position.alt
            assert_type(get_args(Union[int, float]), lat)
            assert_type(get_args(Union[int, float]), lon)
            assert_type(get_args(Union[int, float]), alt)
        return LatLonAlt(lat, lon, alt)

    def _alt_from_vehicle_local_position(self) -> Optional[float]:
        """Returns altitude from vehicle local position or None if not available.

        This method tries to return the 'z' value first, and 'dist_bottom' second from the VehicleLocalPosition
        message. If neither are valid, a None is returned.

        :return: Altitude in meters or None if information is not available"""
        if self._vehicle_local_position is not None:
            if self._vehicle_local_position.z_valid:
                self.get_logger().debug('Using VehicleLocalPosition.z for altitude.')
                return abs(self._vehicle_local_position.z)
            elif self._vehicle_local_position.dist_bottom_valid:
                self.get_logger().debug('Using VehicleLocalPosition.dist_bottom for altitude.')
                return abs(self._vehicle_local_position.dist_bottom)
            else:
                return None
        else:
            return None

    def _latlonalt_from_initial_guess(self) ->  Tuple[Optional[float], Optional[float], Optional[float]]:
        """Returns lat, lon (WGS84) and altitude (meters) from provided values, or None if not available.

        If some of the initial guess values are not provided, a None is returned in their place within the tuple.

        :return: A lat, lon, alt tuple"""
        return self.get_parameter('map_update.initial_guess.lat').get_parameter_value().double_value, \
               self.get_parameter('map_update.initial_guess.lon').get_parameter_value().double_value, \
               self.get_parameter('map_update.default_altitude').get_parameter_value().double_value

    def _map_update_timer_callback(self) -> None:
        """Attempts to update the stored map at regular intervals.

        Calls :meth:`~_update_map` if the center and altitude coordinates for the new map raster are available and the
        :meth:`~_should_update_map` check passes.

        Since knowledge of precise position (WGS84) nor altitude of the vehicle are not important for updating the map
        center, this method tries several ways to get a reasonable estimate for the 'rough' positioning of the vehicle
        in the following order:
            1. Try to get a global position from latest VehicleGlobalPosition message (lat, lon, alt)
            2. Try to get a global position from latest VehicleLocalPosition message (ref_lat, ref_lon, z/dist_bottom)
            3. Try to get a global position from provided initial guess and default altitude

        Finally, if gimbal projection is enabled, this method computes the center of the projected camera field of view
        and retrieves the map for that location instead of the vehicle location to ensure the field of view is contained
        in the map raster.

        :return:
        """
        # Try to get lat, lon, alt from VehicleGlobalPosition if available
        latlonalt = self._latlonalt_from_vehicle_global_position()
        assert_type(LatLonAlt, latlonalt)

        # If altitude was not available in VehicleGlobalPosition, try to get it from VehicleLocalPosition
        if latlonalt.alt is None:
            self.get_logger().debug('Could not get altitude from VehicleGlobalPosition - trying VehicleLocalPosition '
                                    'instead.')
            latlonalt = LatLonAlt(latlonalt.lat, latlonalt.lon, self._alt_from_vehicle_local_position())

        # Try to get Lat and Lon estimate from previous visually estimated position
        # TODO: get x and y it from _image_frame instead of latest published message!
        #  Position/full image_frame needs to be saved even if publish never happens
        if latlonalt.lat is None or latlonalt.lon is None:
            if self._vehicle_visual_odometry is not None:
                if self._local_origin is None:
                    # TODO: compute it from latest position estimate? This should already have been done in _process_matches
                    return  # TODO: remove this return statement once image_frame is saved and _local_frame is computed here
                assert_type(get_args(Union[LatLon, LatLonAlt]), self._local_origin)
                assert hasattr(self._vehicle_visual_odometry, 'x') and hasattr(self._vehicle_visual_odometry, 'y')
                dx, dy = self._vehicle_visual_odometry.x, self._vehicle_visual_odometry.y
                distance = math.sqrt(dx**2 + dy**2)
                azmth = self._get_azimuth(dy, dx)  # NED, so flip x and y axes here
                latlonalt = LatLonAlt(
                    *(self._move_distance(self._local_origin, (azmth, distance)) + (latlonalt.alt, ))
                )

        # If some of latlonalt are still None, try to get from provided initial guess and default alt
        if not all(latlonalt):
            # Warn, not debug, since this is a static guess
            self.get_logger().warn('Could not get (lat, lon, alt) tuple from VehicleGlobalPosition nor '
                                   'VehicleLocalPosition, checking if initial guess has been provided.')
            latlonalt_guess = self._latlonalt_from_initial_guess()
            latlonalt = tuple(latlonalt[i] if latlonalt[i] is not None else latlonalt_guess[i] for i in
                              range(len(latlonalt)))
            latlonalt = LatLonAlt(*latlonalt)

        # Cannot determine vehicle global position
        if not all(latlonalt):
            self.get_logger().warn(f'Could not determine vehicle global position (latlonalt: {latlonalt}) and therefore'
                                   f' cannot update map.')
            return

        # Project principal point if required
        if self._use_gimbal_projection():
            fov_center_ = self._projected_field_of_view_center(latlonalt)
            if fov_center_ is None:
                self.get_logger().warn('Could not project field of view center. Using vehicle position for map center '
                                       'instead.')
            else:
                # Position at camera altitude but above the projected field of view center
                latlonalt = LatLonAlt(fov_center_.lat, fov_center_.lon, latlonalt.alt)

        # Get map size based on altitude and update map if needed
        map_radius = self._get_dynamic_map_radius(latlonalt.alt)
        if self._should_update_map(latlonalt, map_radius):
            self._update_map(latlonalt, map_radius)
        else:
            self.get_logger().debug('Map center and radius not changed enough to update map yet, '
                                    'or previous results are not ready.')

    def _vehicle_visual_odometry_timer_callback(self) -> None:
        """Publishes the vehicle visual odometry message at given intervals.

        This callback publishes the :class:`px4_msgs.msg.VehicleVisualOdometry` message stored in
        :py:attr:`~_vehicle_visual_odometry`. The message is created and stored by the
        :meth:`~_create_vehicle_visual_odometry_msg` method when latest image-to-map matches are successfully processed
        by :meth:`~_process_matches`. The match processing is always triggered by new matching results arriving via
        :meth:`~_superglue_pool_worker_callback`.

        :return:
        """
        if self._vehicle_visual_odometry is not None:
            assert_type(VehicleVisualOdometry, self._vehicle_visual_odometry)
            now = time.time_ns()
            if self._publish_timestamp is not None:
                assert now > self._publish_timestamp
                hz = 1e9 / (now - self._publish_timestamp)
                self.get_logger().debug(
                    f'Publishing vehicle visual odometry message:\n{self._vehicle_visual_odometry}. '
                    f'Publish frequency {hz} Hz.')

                # Warn if we are close to the bounds of acceptable frequency range
                warn_padding = self.VVO_PUBLISH_FREQUENCY_WARNING_PADDING
                if not self.MINIMUM_PUBLISH_FREQUENCY + warn_padding < hz < self.MAXIMUM_PUBLISH_FREQUENCY - warn_padding:
                    self.get_logger().warn(f'Publish frequency {hz} Hz is close to or outside of bounds of required '
                                           f'frequency range of [{self.MINIMUM_PUBLISH_FREQUENCY}, '
                                           f'{self.MAXIMUM_PUBLISH_FREQUENCY}] Hz for EKF2 fusion.')

            self._publish_timestamp = now
            self._topics.get(self.PUBLISH_KEY).get(self.VEHICLE_VISUAL_ODOMETRY_TOPIC_NAME)\
                .publish(self._vehicle_visual_odometry)
        else:
            self.get_logger().debug('Vehicle visual odometry publishing timer triggered but there was nothing to '
                                    'publish.')

    def _declare_ros_params(self, config: dict) -> None:
        """Declares ROS parameters from a config file.

        Uses defaults from :py:mod:`python_px4_ros2_map_nav.ros_param_defaults` if values are not provided. Note that
        some parameters are declared as read_only and cannot be changed at runtime.

        :param config: The value of the ros__parameters key from the parsed configuration file.
        :return:
        """
        namespace = 'wms'
        self.declare_parameters(namespace, [
            ('url', config.get(namespace, {}).get('url', Defaults.WMS_URL), ParameterDescriptor(read_only=True)),
            ('version', config.get(namespace, {})
             .get('version', Defaults.WMS_VERSION), ParameterDescriptor(read_only=True)),
            ('layer', config.get(namespace, {}).get('layer', Defaults.WMS_LAYER)),
            ('srs', config.get(namespace, {}).get('srs', Defaults.WMS_SRS)),
            ('request_timeout', config.get(namespace, {}).get('request_timeout', Defaults.WMS_REQUEST_TIMEOUT))
        ])

        namespace = 'misc'
        self.declare_parameters(namespace, [
            ('affine_threshold', config.get(namespace, {}).get('affine_threshold', Defaults.MISC_AFFINE_THRESHOLD)),
            ('publish_frequency', config.get(namespace, {})
             .get('publish_frequency', Defaults.MISC_PUBLISH_FREQUENCY), ParameterDescriptor(read_only=True)),
            ('export_position', config.get(namespace, {}).get('export_position', Defaults.MISC_EXPORT_POSITION)),
            ('export_projection', config.get(namespace, {}).get('export_projection', Defaults.MISC_EXPORT_PROJECTION)),
            ('max_pitch', config.get(namespace, {}).get('max_pitch', Defaults.MISC_MAX_PITCH)),
            ('visualize_homography', config.get(namespace, {})
             .get('visualize_homography', Defaults.MISC_VISUALIZE_HOMOGRAPHY)),
            ('covariance_estimation_length', config.get(namespace, {})
             .get('covariance_estimation_length', Defaults.MISC_COVARIANCE_ESTIMATION_LENGTH)),
        ])

        namespace = 'map_update'
        self.declare_parameters(namespace, [
            ('initial_guess.lat', config.get(namespace, {}).get('initial_guess', {})
             .get('lat', Defaults.MAP_UPDATE_INITIAL_GUESS.lat)),
            ('initial_guess.lon', config.get(namespace, {}).get('initial_guess', {})
             .get('lon', Defaults.MAP_UPDATE_INITIAL_GUESS.lon)),
            ('update_delay', config.get(namespace, {})
             .get('update_delay', Defaults.MAP_UPDATE_UPDATE_DELAY), ParameterDescriptor(read_only=True)),
            ('default_altitude', config.get(namespace, {})
             .get('default_altitude', Defaults.MAP_UPDATE_DEFAULT_ALTITUDE)),
            ('gimbal_projection', config.get(namespace, {})
             .get('gimbal_projection', Defaults.MAP_UPDATE_GIMBAL_PROJECTION)),
            ('max_map_radius', config.get(namespace, {}).get('max_map_radius', Defaults.MAP_UPDATE_MAX_MAP_RADIUS)),
            ('map_radius_meters_default', config.get(namespace, {})
             .get('map_radius_meters_default', Defaults.MAP_UPDATE_MAP_RADIUS_METERS_DEFAULT)),
            ('update_map_center_threshold', config.get(namespace, {})
             .get('update_map_center_threshold', Defaults.MAP_UPDATE_UPDATE_MAP_CENTER_THRESHOLD)),
            ('update_map_radius_threshold', config.get(namespace, {})
             .get('update_map_radius_threshold', Defaults.MAP_UPDATE_UPDATE_MAP_RADIUS_THRESHOLD)),
            ('max_pitch', config.get(namespace, {}).get('max_pitch', Defaults.MAP_UPDATE_MAX_PITCH))
        ])

    def _load_config(self, yaml_file: str) -> dict:
        """Loads config from the provided YAML file.

        :param yaml_file: Path to the yaml file
        :return: The loaded yaml file as dictionary
        """
        assert_type(str, yaml_file)
        with open(os.path.join(self._share_dir, yaml_file), 'r') as f:
            try:
                config = yaml.safe_load(f)
                self.get_logger().info(f'Loaded config:\n{config}.')
                return config
            except Exception as e:
                self.get_logger().error(f'Could not load config file {yaml_file} because of exception:'
                                        f'\n{e}\n{traceback.print_exc()}')

    def _use_gimbal_projection(self) -> bool:
        """Checks if map rasters should be retrieved for projected field of view instead of vehicle position.

        If this is set to false, map rasters are retrieved for the vehicle's global position instead. This is typically
        fine as long as the camera is not aimed too far in to the horizon and has a relatively wide field of view. For
        best results, this should be on to ensure the field of view is fully contained within the area of the retrieved
        map raster.

        :return: True if field of view projection should be used for updating map rasters
        """
        gimbal_projection_flag = self.get_parameter('map_update.gimbal_projection').get_parameter_value().bool_value
        if type(gimbal_projection_flag) is bool:
            return gimbal_projection_flag
        else:
            self.get_logger().warn(f'Could not read gimbal projection flag: {gimbal_projection_flag}. Assume False.')
            return False

    def _sync_timestamps(self, ekf2_timestamp_usec: int) -> None:
        """Synchronizes local timestamp with EKF2's system time.

        This synchronization is done in the :meth:`~vehicle_local_position_callback`. The sync is therefore expected
        to be done at high frequency.

        See :py:attr:`~_time_sync` for more information.

        :param ekf2_timestamp_usec: The time since the EKF2 system start in microseconds
        :return:
        """
        assert_type(int, ekf2_timestamp_usec)
        now_usec = time.time() * 1e6
        self._time_sync = TimePair(now_usec, ekf2_timestamp_usec)

    def _get_ekf2_time(self) -> Optional[int]:
        """Returns current (estimated) EKF2 timestamp in microseconds

        See :py:attr:`~_time_sync` for more information.

        :return: Estimated EKF2 system time in microseconds or None if not available"""
        if self._time_sync is None:
            self.get_logger().warn('Could not estimate EKF2 timestamp.')
            return None
        else:
            now_usec = time.time() * 1e6
            assert now_usec > self._time_sync.local, f'Current timestamp {now_usec} was unexpectedly smaller than ' \
                                                     f'timestamp stored earlier for synchronization ' \
                                                     f'{self._time_sync.local}.'
            ekf2_timestamp_usec = int(self._time_sync.foreign + (now_usec - self._time_sync.local))
            return ekf2_timestamp_usec + self.EKF2_TIMESTAMP_PADDING  # TODO: remove the padding or set it 0?

    def _restrict_affine(self) -> bool:
        """Checks if homography matrix should be restricted to an affine transformation (nadir facing camera).

        The field of view estimate is more stable if an affine transformation can be assumed. This also leads to the
        estimated position of the vehicle being more stable. For a better positioning estimate a nadir-facing camera
        should be assumed and the homography estimation restricted for 2D affine transformations. See implementation of
        :meth:`~_find_and_decompose_homography` for how this flag is used to determine how the homography matrix between
        the image and map rasters is estimated.

        :return: True if homography matrix should be restricted to a 2D affine transformation.
        """
        restrict_affine_threshold = self.get_parameter('misc.affine_threshold').get_parameter_value().integer_value
        assert_type(get_args(Union[int, float]), restrict_affine_threshold)
        camera_pitch = self._camera_pitch()
        if camera_pitch is not None:
            if abs(camera_pitch) <= restrict_affine_threshold:
                return True
            else:
                return False
        else:
            self.get_logger().warn(f'Could not get camera pitch - cannot assume affine 2D transformation.')
            return False

    def _setup_topics(self) -> None:
        """Creates and stores publishers and subscribers for microRTPS bridge topics.

        :return:
        """
        for topic in self.TOPICS:
            topic_name = topic.get(self.TOPIC_NAME_KEY, None)
            class_ = topic.get(self.CLASS_KEY, None)
            assert topic_name is not None, f'Topic name not provided in topic: {topic}.'
            assert class_ is not None, f'Class not provided in topic: {topic}.'

            publish = topic.get(self.PUBLISH_KEY, None)
            if publish is not None:
                assert_type(bool, publish)
                self._topics.get(self.PUBLISH_KEY).update({topic_name: self._create_publisher(topic_name, class_)})

            subscribe = topic.get(self.SUBSCRIBE_KEY, None)
            if subscribe is not None:
                assert_type(bool, subscribe)
                self._topics.get(self.SUBSCRIBE_KEY).update({topic_name: self._create_subscriber(topic_name, class_)})

        self.get_logger().info(f'Topics setup complete:\n{self._topics}.')

    def _create_publisher(self, topic_name: str, class_: object) -> rclpy.publisher.Publisher:
        """Sets up an rclpy publisher.

        :param topic_name: Name of the microRTPS topic
        :param class_: Message definition class (e.g. px4_msgs.msg.VehicleVisualOdometry)
        :return: The publisher instance
        """
        return self.create_publisher(class_, topic_name, self.PUBLISH_QOS_PROFILE)

    def _create_subscriber(self, topic_name: str, class_: object) -> rclpy.subscription.Subscription:
        """Sets up an rclpy subscriber.

        :param topic_name: Name of the microRTPS topic
        :param class_: Message definition class (e.g. px4_msgs.msg.VehicleLocalPosition)
        :return: The subscriber instance
        """
        callback_name = topic_name.lower() + '_callback'
        callback = getattr(self, callback_name, None)
        assert callback is not None, f'Missing callback implementation for {callback_name}.'
        return self.create_subscription(class_, topic_name, callback, 10)  # TODO: add explicit QoSProfile

    def _get_bbox(self, latlon: Union[LatLon, LatLonAlt], radius_meters: Optional[Union[int, float]] = None) -> BBox:
        """Gets the bounding box containing a circle with given radius centered at given lat-lon fix.

        If the map radius is not provided, a default value is used.

        :param latlon: Center of the bounding box
        :param radius_meters: Radius of the circle in meters enclosed by the bounding box
        :return: The bounding box
        """
        if radius_meters is None:
            radius_meters = self.get_parameter('map_update.map_radius_meters_default')\
                .get_parameter_value().integer_value
        assert_type(get_args(Union[LatLon, LatLonAlt]), latlon)
        assert_type(get_args(Union[int, float]), radius_meters)
        corner_distance = math.sqrt(2) * radius_meters  # Distance to corner of square enclosing circle of radius
        ul = self._move_distance(latlon, (-45, corner_distance))
        lr = self._move_distance(latlon, (135, corner_distance))
        return BBox(ul.lon, lr.lat, lr.lon, ul.lat)

    def _get_distance_of_fov_center(self, fov_wgs84: np.ndarray) -> float:
        """Calculate distance between middle of sides of field of view (FOV) based on triangle similarity.

        This is used when estimating camera distance from estimated principal point using triangle similarity in
        :meth:`~_compute_camera_distance`.

        :param fov_wgs84: The WGS84 corner coordinates of the FOV
        :return: Camera distance in meters from its principal point projected onto ground
        """
        # TODO: assert shape of fov_wgs84
        midleft = ((fov_wgs84[0] + fov_wgs84[1]) * 0.5).squeeze()
        midright = ((fov_wgs84[2] + fov_wgs84[3]) * 0.5).squeeze()
        _, __, dist = self._geod.inv(midleft[1], midleft[0], midright[1], midright[0])  # TODO: use distance method here
        return dist

    def _distance(self, latlon1: Union[LatLon, LatLonAlt], latlon2: Union[LatLon, LatLonAlt]) -> float:
        """Returns distance between two points in meters.

        The distance computation is based on latitude and longitude only and ignores altitude.

        :param latlon1: The first point
        :param latlon2: The second point
        :return: The ground distance in meters between the two points
        """
        assert_type(get_args(Union[LatLon, LatLonAlt]), latlon1)
        assert_type(get_args(Union[LatLon, LatLonAlt]), latlon2)
        _, __, dist = self._geod.inv(latlon1.lon, latlon1.lat, latlon2.lon, latlon2.lat)
        return dist

    def _move_distance(self, latlon: Union[LatLon, LatLonAlt], azmth_dist: Tuple[Union[int, float], Union[int, float]])\
            -> LatLon:
        """Returns the point that is a given distance in the direction of azimuth from the origin point.

        :param latlon: Origin point
        :param azmth_dist: Tuple containing azimuth in degrees and distance in meters: (azimuth, distance)
        :return: The point that is given meters away in the azimuth direction from origin
        """
        assert_type(tuple, azmth_dist)
        assert_type(get_args(Union[LatLon, LatLonAlt]), latlon)
        azmth, dist = azmth_dist  # TODO: silly way of providing these args just to map over a zipped list in _update_map, fix it
        assert_type(get_args(Union[int, float]), azmth)
        assert_type(get_args(Union[int, float]), dist)
        lon, lat, azmth = self._geod.fwd(latlon.lon, latlon.lat, azmth, dist)
        return LatLon(lat, lon)

    def _map_size_with_padding(self) -> Optional[Tuple[int, int]]:
        """Returns map size with padding for rotation without clipping corners.

        Because the deep learning models used for predicting matching keypoints between camera image frames and
        retrieved map rasters are not assumed to be rotation invariant, the map rasters are rotated based on camera yaw
        so that they align with the camera images. To keep the scale of the map after rotation the same, black corners
        would appear unless padding is used. Retrieved maps therefore have to squares with the side lengths matching the
        diagonal of the camera frames so that scale is preserved and no black corners appear in the map rasters after
        rotation.

        :return: Padded map size tuple (height, width) or None if the info is not available. The height and width will
        both be equal to the diagonal of the declared (:py:attr:`~_camera_info`) camera frame dimensions.
        """
        dim = self._img_dim()
        if dim is None:
            self.get_logger().warn(f'Dimensions not available - returning None as map size.')
            return None
        assert_type(Dim, dim)
        diagonal = math.ceil(math.sqrt(dim.width ** 2 + dim.height ** 2))
        assert_type(int, diagonal)  # TODO: What if this is float?
        return diagonal, diagonal

    def _map_dim_with_padding(self) -> Optional[Dim]:
        """Returns map dimensions with padding for rotation without clipping corners.

        This method is a wrapper for :meth:`~map_size_with_padding`.

        :return: Map dimensions or None if the info is not available
        """
        map_size = self._map_size_with_padding()
        if map_size is None:
            self.get_logger().warn(f'Map size with padding not available - returning None as map dimensions.')
            return None
        assert_type(tuple, map_size)
        assert_len(map_size, 2)
        return Dim(*map_size)

    def _declared_img_size(self) -> Optional[Tuple[int, int]]:
        """Returns image resolution size as it is declared in the latest CameraInfo message.

        :return: Image resolution tuple (height, width) or None if not available
        """
        if self._camera_info is not None:
            # TODO: assert or check hasattr?
            return self._camera_info.height, self._camera_info.width  # numpy order: h, w, c --> height first
        else:
            self.get_logger().warn('Camera info was not available - returning None as declared image size.')
            return None

    def _img_dim(self) -> Optional[Dim]:
        """Returns image dimensions as it is declared in the latest CameraInfo message.

        This method is a wrapper for :meth:`~declared_img_size`.

        :return: Image dimensions or None if not available
        """
        declared_size = self._declared_img_size()
        if declared_size is None:
            self.get_logger().warn('CDeclared size not available - returning None as image dimensions.')
            return None
        assert_type(tuple, declared_size)
        assert_len(declared_size, 2)
        return Dim(*declared_size)

    def _project_gimbal_fov(self, translation: np.ndarray) -> Optional[np.ndarray]:
        """Returns field of view (FOV) meter coordinates projected using gimbal attitude and camera intrinsics.

        The returned fov coordinates are meters from the origin of projection of the FOV on ground. This method is used
        by :meth:`~_projected_field_of_view_center` when new coordinates for an outgoing WMS GetMap request are needed.

        :param translation: Translation vector (cx, cy, altitude) in meter coordinates
        :return: Projected FOV bounding box in pixel coordinates or None if not available
        """
        assert_shape(translation, (3,))
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Could not get RPY - cannot project gimbal fov.')
            return None

        r = Rotation.from_euler(self.EULER_SEQUENCE, list(rpy), degrees=True).as_matrix()
        e = np.hstack((r, np.expand_dims(translation, axis=1)))
        assert_shape(e, (3, 4))

        if self._camera_info is None:
            self.get_logger().warn('Could not get camera info - cannot project gimbal fov.')
            return None
        h, w = self._img_dim()
        # TODO: assert h w not none and integers? and divisible by 2?

        # Intrinsic matrix
        k = np.array(self._camera_info.k).reshape([3, 3])

        # Project image corners to z=0 plane (ground)
        src_corners = create_src_corners(h, w)
        assert_shape(src_corners, (4, 1, 2))
        e = np.delete(e, 2, 1)  # Remove z-column, making the matrix square
        p = np.matmul(k, e)
        try:
            p_inv = np.linalg.inv(p)
        except np.linalg.LinAlgError as e:
            self.get_logger().error(f'Could not invert the projection matrix: {p}. RPY was {rpy}. Trace:'
                                    f'\n{e},\n{traceback.print_exc()}.')
            return None

        assert_shape(p_inv, (3, 3))

        dst_corners = cv2.perspectiveTransform(src_corners, p_inv)  # TODO: use util.get_fov here?
        assert_shape(dst_corners, src_corners.shape)
        dst_corners = dst_corners.squeeze()  # TODO: See get_fov usage elsewhere -where to do squeeze if at all?

        return dst_corners

    def _vehicle_local_position_ref_latlonalt(self) -> Optional[LatLonAlt]:
        """Returns vehicle local frame reference origin

        :return: Local reference frame origin in WGS84, or None if not available
        """
        if self._vehicle_local_position is None:
            self.get_logger().warn('Could not get vehicle local position - returning None as local frame reference.')
            return None

        if self._vehicle_local_position.xy_global is True and self._vehicle_local_position.z_global is True:
            assert_type(int, self._vehicle_local_position.timestamp)
            return LatLonAlt(self._vehicle_local_position.ref_lat, self._vehicle_local_position.ref_lon,
                             self._vehicle_local_position.ref_alt)
        else:
            # TODO: z may not be needed - make a separate _ref_latlon method!
            self.get_logger().warn('No valid global reference for local frame origin - returning None.')
            return None

    @staticmethod
    def _get_azimuth(x: float, y: float) -> float:
        """Get azimuth of position x and y coordinates.

        Note: in NED coordinates x is north, so here it would be y.

        :param x: Meters towards east
        :param y: Meters towards north

        :return: Azimuth in degrees
        """
        rads = math.atan2(y, x)
        rads = rads if rads > 0 else rads + 2*math.pi  # Counter-clockwise from east
        rads = -rads + math.pi/2  # Clockwise from north
        return math.degrees(rads)

    def _projected_field_of_view_center(self, origin: LatLonAlt) -> Optional[LatLon]:
        """Returns WGS84 coordinates of projected camera field of view (FOV).

        Used in :meth:`~_map_update_timer_callback` when gimbal projection is enabled to determine center coordinates
        for next WMS GetMap request.

        :param origin: Camera position  # TODO: why is this an argument but all else is not?
        :return: Center of the FOV or None if not available
        """
        if self._camera_info is not None:
            pitch = self._camera_pitch()  # TODO: _project_gimbal_fov uses _get_camera_rpy - redundant calls
            if pitch is None:
                self.get_logger().warn('Camera pitch not available, cannot project gimbal field of view.')
                return None
            assert 0 <= abs(pitch) <= 90, f'Pitch {pitch} was outside of expected bounds [0, 90].' # TODO: need to handle outside of bounds, cannot assert
            pitch_rad = math.radians(pitch)
            assert origin.alt is not None
            assert hasattr(origin, 'alt')
            hypotenuse = origin.alt * math.tan(pitch_rad)  # Distance from camera origin to projected principal point
            cx = hypotenuse*math.sin(pitch_rad)
            cy = hypotenuse*math.cos(pitch_rad)
            translation = np.array([-cx, -cy, origin.alt])
            gimbal_fov_pix = self._project_gimbal_fov(translation)

            # Convert gimbal field of view from pixels to WGS84 coordinates
            if gimbal_fov_pix is not None:
                azmths = list(map(lambda x: self._get_azimuth(x[0], x[1]), gimbal_fov_pix))
                dists = list(map(lambda x: math.sqrt(x[0] ** 2 + x[1] ** 2), gimbal_fov_pix))
                zipped = list(zip(azmths, dists))
                to_wgs84 = partial(self._move_distance, origin)
                gimbal_fov_wgs84 = np.array(list(map(to_wgs84, zipped)))
                ### TODO: add some sort of assertion hat projected FoV is contained in size and makes sense

                # Use projected field of view center instead of global position as map center
                map_center_latlon = fov_center(gimbal_fov_wgs84)  # TODO: use cx, cy and not fov corners, polygon center != principal point

                # Export to file in GIS readable format
                export_projection = self.get_parameter('misc.export_projection').get_parameter_value().string_value
                if export_projection is not None:
                    self._export_position(map_center_latlon, gimbal_fov_wgs84, export_projection)
            else:
                self.get_logger().warn('Could not project camera FoV, getting map raster assuming nadir-facing camera.')
                return None
        else:
            self.get_logger().debug('Camera info not available, cannot project FoV, defaulting to global position.')
            return None

        return map_center_latlon  # TODO: using principal point for updating map no good, too close to bottom fov. Principal point still needed but not for updating map.

    def _update_map(self, center: Union[LatLon, LatLonAlt], radius: Union[int, float]) -> None:
        """Instructs the WMS client to get a new map from the WMS server.

        :param center: WGS84 coordinates of map to be retrieved
        :param radius: Radius in meters of circle to be enclosed by the map raster
        :return:
        """
        self.get_logger().info(f'Updating map at {center}, radius {radius} meters.')
        assert_type(get_args(Union[LatLon, LatLonAlt]), center)
        assert_type(get_args(Union[int, float]), radius)
        max_radius = self.get_parameter('map_update.max_map_radius').get_parameter_value().integer_value
        # TODO: need to recover from this, e.g. if its more than max_radius, warn and use max instead. Users could crash this by setting radius to above max radius
        assert 0 < radius <= max_radius, f'Radius should be between 0 and {max_radius}.'

        bbox = self._get_bbox(center, radius)  # TODO: should these things be moved to args? Move state related stuff up the call stack all in the same place. And isnt this a static function anyway?
        assert_type(BBox, bbox)

        map_size = self._map_size_with_padding()
        if map_size is None:
            self.get_logger().warn('Map size not yet available - skipping WMS request.')
            return None

        # Build and send WMS request
        url = self.get_parameter('wms.url').get_parameter_value().string_value
        version = self.get_parameter('wms.version').get_parameter_value().string_value
        layer_str = self.get_parameter('wms.layer').get_parameter_value().string_value
        srs_str = self.get_parameter('wms.srs').get_parameter_value().string_value
        assert_type(str, url)
        assert_type(str, version)
        assert_type(str, layer_str)
        assert_type(str, srs_str)
        try:
            self.get_logger().info(f'Getting map for bbox: {bbox}, layer: {layer_str}, srs: {srs_str}.')
            if self._wms_results is not None:
                assert self._wms_results.ready(), f'Update map was called while previous results were not yet ready.'  # Should not happen - check _should_update_map conditions
            timeout = self.get_parameter('wms.request_timeout').get_parameter_value().integer_value
            self._wms_results = self._wms_pool.starmap_async(
                self._wms_pool_worker, [(LatLon(center.lat, center.lon), radius, bbox, map_size, url, version,  # TODO: conersion of center to LatLon may be redundant?
                                         layer_str, srs_str, timeout)],
                callback=self.wms_pool_worker_callback, error_callback=self.wms_pool_worker_error_callback)
        except Exception as e:
            self.get_logger().error(f'Something went wrong with WMS worker:\n{e},\n{traceback.print_exc()}.')
            return None

    def wms_pool_worker_callback(self, result: List[MapFrame]) -> None:
        """Handles result from WMS pool worker.

        Saves received :class:`util.MapFrame` to :py:attr:`~_map_frame.

        :param result: Results from the asynchronous call (a collection containing a single :class:`util.MapFrame`)
        :return:
        """
        assert_len(result, 1)
        result = result[0]
        self.get_logger().info(f'WMS callback for bbox: {result.bbox}.')
        assert_type(MapFrame, result)
        if self._map_frame is not None:
            self._previous_map_frame = self._map_frame
        self._map_frame = result
        assert self._map_frame.image.shape[0:2] == self._map_size_with_padding(), \
            'Decoded map is not the specified size.'  # TODO: make map size with padding an argument?

    def wms_pool_worker_error_callback(self, e: BaseException) -> None:
        """Handles errors from WMS pool worker.

        :param e: Exception returned by the worker
        :return:
        """
        self.get_logger().error(f'Something went wrong with WMS process:\n{e},\n{traceback.print_exc()}.')

    @staticmethod
    def _wms_pool_worker(center: LatLon, radius: Union[int, float], bbox: BBox,
                         map_size: Tuple[int, int], url: str, version: str, layer_str: str, srs_str: str, timeout: int)\
            -> MapFrame:
        """Gets latest map from WMS server for given location, then creates a :class:`util.MapFrame` and returns it

        :param center: Center of the map to be retrieved
        :param radius: Radius in meters of the circle to be enclosed by the map
        :param bbox: Bounding box of the map
        :param map_size: Map size tuple (height, width)
        :param url: WMS server url
        :param version: WMS server version
        :param layer_str: WMS server layer
        :param srs_str: WMS server SRS
        :param timeout: WMS client request timeout in seconds
        :return: MapFrame containing the map raster and supporting metadata
        """
        """"""
        # TODO: computation of bbox could be pushed in here - would just need to make Matcher._get_bbox pickle-able
        assert_type(str, url)
        assert_type(str, version)
        assert_type(int, timeout)
        wms_client = _cached_wms_client(url, version, timeout)
        assert wms_client is not None
        assert_type(BBox, bbox)
        assert(all(isinstance(x, int) for x in map_size))
        assert_type(str, layer_str)
        assert_type(str, srs_str)
        assert_type(LatLon, center)
        assert_type(get_args(Union[int, float]), radius)
        try:
            map_ = wms_client.getmap(layers=[layer_str], srs=srs_str, bbox=bbox, size=map_size, format='image/png',
                                     transparent=True)
            # TODO: what will map_ be if the reqeust times out? will an error be raised?
        except Exception as e:
            raise e  # TODO: need to do anything here or just pass it on?

        # Decode response from WMS server
        map_ = np.frombuffer(map_.read(), np.uint8)
        map_ = cv2.imdecode(map_, cv2.IMREAD_UNCHANGED)
        assert_type(np.ndarray, map_)
        assert_ndim(map_, 3)
        map_frame = MapFrame(center, radius, bbox, map_)
        return map_frame

    @staticmethod
    def _superglue_init_worker(config: dict):
        """Initializes SuperGlue in a dedicated process.

        The SuperGlue instance is stored into a global variable inside its own dedicated process to avoid
        re-instantiating it every time the model is needed.

        :param config: SuperGlue config
        :return:
        """
        superglue_conf = config.get('superglue', None)
        assert_type(dict, superglue_conf)
        global superglue
        superglue = SuperGlue(superglue_conf)

    @staticmethod
    def _superglue_pool_worker(img: np.ndarray, map_: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Finds matching keypoints between input images.

        :param img: The first image
        :param map_: The second image
        :return: Tuple of two lists containing matching keypoints in img and map, respectively
        """
        """"""
        assert_type(np.ndarray, img)
        assert_type(np.ndarray, map_)
        try:
            return superglue.match(img, map_)
        except Exception as e:
            raise e  # TODO: need to do anything here or just pass it on?

    def image_raw_callback(self, msg: Image) -> None:
        """Handles latest image frame from camera.

        For every image frame, uses :meth:`~_should_match` to determine whether a new :meth:`_match` call needs to be
        made to the neural network. Inputs for the :meth:`_match` call are collected with :meth:`~_match_inputs` and
        saved into :py:attr:`~_stored_inputs` for later use. When the match call returns,
        the :meth:`~_superglue_worker_callback` will use the stored inputs for post-processing the matches based on
        the same snapshot of data that was used to make the call. It is assumed that the latest stored inputs are the
        same ones that were used for making the :meth:`_match` call, no additional checking or verification is used.

        :param msg: The Image message from the PX4-ROS 2 bridge to decode
        :return:
        """
        # Estimate EKF2 timestamp first to get best estimate
        timestamp = self._get_ekf2_time()
        if timestamp is None:
            self.get_logger().warn('Image frame received but could not estimate EKF2 system time, skipping frame.')
            return None

        self.get_logger().debug('Camera image callback triggered.')
        assert_type(Image, msg)

        cv_image = self._cv_bridge.imgmsg_to_cv2(msg, self.IMAGE_ENCODING)

        # Check that image dimensions match declared dimensions
        img_size = self._declared_img_size()
        if img_size is not None:
            cv_img_shape = cv_image.shape[0:2]
            assert cv_img_shape == img_size, f'Converted cv_image shape {cv_img_shape} did not match declared image ' \
                                             f'shape {img_size}.'

        # Process image frame
        #print(f'timestamp {timestamp}')
        # TODO: save previous image frame and check that new timestamp is greater
        image_frame = ImageFrame(cv_image, msg.header.frame_id, timestamp)

        # TODO: store image_frame as self._image_frame and move the stuff below into a dedicated self._matching_timer?
        if self._should_match():
            assert self._superglue_results is None or self._superglue_results.ready()
            inputs = self._match_inputs(image_frame)
            for k, v in inputs.items():
                if v is None:
                    if k not in ['local_frame_origin_position', 'timestamp']:  # TODO: use self._local_origin here to get rid of this clause?
                        self.get_logger().warn(f'Key {k} value {v} in match input arguments, cannot process matches.')
                        return

            camera_yaw = inputs.get('camera_yaw', None)
            map_frame = inputs.get('map_frame', None)
            img_dim = inputs.get('img_dim', None)
            assert all((camera_yaw, map_frame, img_dim))  # Redundant (see above 'for k, v in inputs.items(): ...')

            self._stored_inputs = inputs
            map_cropped = inputs.get('map_cropped')
            assert_type(np.ndarray, map_cropped)

            self.get_logger().debug(f'Matching image with timestamp {image_frame.timestamp} to map.')
            self._match(image_frame, map_cropped)

    def _camera_yaw(self) -> Optional[int]:  # TODO: int or float?
        """Returns camera yaw in degrees.

        :return: Camera yaw in degrees, or None if not available
        """
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn(f'Could not get camera RPY - cannot return yaw.')
            return None
        assert_type(RPY, rpy)
        camera_yaw = rpy.yaw
        return camera_yaw

    def _transform_rotation_axes(self, rotation: Rotation):
        """Transform the axes of rotation so that they match the axes of PX4's rotations."""
        # TODO: need to make the Rotation from rotation matrix from decomposeHomography compatible with vheicle attitude etc. Rotation
        raise NotImplementedError

    @staticmethod
    def _estimate_gimbal_attitude(vehicle_ned_attitude: Rotation, gimbal_ned_attitude_estimate: Rotation):
        """Estimates gimbal relative attitude in vehicle FRD body frame from visually estimate camera NED fixed frame
        attitude

        This method solves the relative gimbal attitude from visually estimated gimbal absolute attitude. When the
        vehicle has started moving, the gimbal needs a short time to stabilize and before that happens there may be a
        large inconsistency between gimbal settings and true attitude. The attitude cannot therefore be trusted from
        :class:`px4_msgs.msg.GimbalDeviceSetAttitude` and it is assumed :class:`px4_msgs.msg.GimbalDeviceAttitudeStatus`
        generally is not available.

        Estimating the true gimbal attitude from visual information is important because small changes in true gimbal
        attitude lead to large changes in estimated vehicle position.
        """
        gimbal_attitude = gimbal_ned_attitude_estimate * vehicle_ned_attitude.inv()
        return gimbal_attitude

    # TODO: try to make static since its used in _process_matches
    def _rotation_to_rpy(self, rotation: Rotation, degrees: bool = False) -> RPY:
        """Returns rotation as roll, pitch, and yaw (RPY) tuple in radians

        :param degrees: True to return RPY in degrees
        :return: Roll, pitch and yaw (RPY) tuple in degrees
        """
        # Convert to Euler angles and re-arrange axes
        assert self.EULER_SEQUENCE_VISUAL == 'xyz'
        euler = rotation.as_euler(self.EULER_SEQUENCE_VISUAL)
        # TODO: these are different from quat_to_rpy and rpy_to_quat which are used for px4's attitude quats!

        roll = euler[2]
        pitch = euler[0] - np.pi/2
        yaw = -euler[1]

        self.get_logger().info(f'_rotation_to_rpy: {roll, pitch, yaw}.')

        # TODO: should these be inclusive of upper bound?
        #assert -np.pi <= roll <= np.pi
        #assert -np.pi/2 <= pitch <= np.pi/2
        #assert 0 <= yaw <= 2*np.pi

        rpy = RPY(roll, pitch, yaw)
        if degrees:
            rpy = RPY(*tuple(map(np.degrees(rpy))))

        return rpy

    # TODO: try to make static since its used in _process_matches
    def _rpy_to_rotation(self, rpy: RPY) -> Rotation:
        """Returns rotation from RPY tuple in radians

        Reverses :meth:`~_rotation_to_rpy`

        :return: Roll, pitch and yaw (RPY) tuple in degrees
        """
        # Convert to Euler angles and re-arrange axes
        assert self.EULER_SEQUENCE_VISUAL == 'xyz'
        # TODO: these are different from quat_to_rpy and rpy_to_quat!
        # Reverse axes transformations
        roll = rpy.roll
        pitch = rpy.pitch + np.pi/2
        yaw = -rpy.yaw

        # Reverse the index order
        euler = [pitch, yaw, roll]

        rotation = rotation.from_euler(self.EULER_SEQUENCE_VISUAL, euler)
        return rotation

    def _get_vehicle_attitude(self) -> Optional[Rotation]:
        """Returns vehicle attitude from :class:`px4_msgs.msg.VehicleAttitude` or None if not available

        :return: Vehicle attitude or None if not available
        """
        if self._vehicle_attitude is None:
            self.get_logger().warn('No VehicleAttitude message has been received yet.')
            return None
        else:
            vehicle_attitude = Rotation.from_quat(self._vehicle_attitude.q)
            return vehicle_attitude

    def _get_gimbal_set_attitude(self) -> Optional[Rotation]:
        """Returns gimbal set attitude from :class:`px4_msgs.msg.GimbalDeviceSetAttitude` or None if not available

        :return: Vehicle attitude or None if not available
        """
        if self._vehicle_attitude is None:
            self.get_logger().warn('No VehicleAttitude message has been received yet.')
            return None
        else:
            vehicle_attitude = Rotation.from_quat(self._vehicle_attitude.q)
            return vehicle_attitude

    def _get_gimbal_set_compound_attitude(self) -> Optional[Rotation]:
        """Returns gimbal set compound attitude or None if it cannot be computed

        Compound attitude means this method compounds the vehicle body frame relative gimbal attitude from
        :class:`px4_msgs.msg.GimbalDeviceSetAttitude` with the NED frame fixed vehicle attitude from
        :class:`px4_msgs.msg.VehicleAttitude`. This is the NED frame fixed attitude that the gimbal should have when it
        is stabilized.

        Because :class:`px4_msgs.msg.GimbalDeviceSetAttitude` is used for gimbal attitude instead of
        :class:`px4_msgs.msg.GimbalDeviceAttitudeStatus`, the output may not necessarily reflect actual camera attitude.
        For example, it takes a fraction of a second for a stabilized gimbal to adjust when the vehicle starts moving.
        When that happens the actual gimbal attitude does not match what is contained in the
        :class:`px4_msgs.msg.GimbalDeviceSetAttitude` message until the gimbal has stabilized again.

        Since :class:`px4_msgs.msg.GimbalDeviceAttitudeStatus` may often not be available so the actual camera attitude
        must be visually estimated in order to also estimate the vehicle's position.

        :return: Gimbal set compound attitude or None if it cannot be computed
        """
        # Get vehicle attitude
        vehicle_attitude = self._get_vehicle_attitude()
        if vehicle_attitude is None:
            self.get_logger().warn('Vehicle attitude not available, cannot compute camera attitude.')
            return None

        # Get gimbal set attitude
        gimbal_set_attitude = self._get_gimbal_set_attitude()
        if gimbal_set_attitude is None:
            self.get_logger().debug('Gimbal set attitude not available, cannot compute camera attitude.')
            return None

        gimbal_set_compound_attitude = vehicle_attitude * gimbal_set_attitude
        return gimbal_set_compound_attitude

    def _get_camera_rpy(self) -> Optional[RPY]:
        """Returns roll-pitch-yaw tuple in NED frame.

        :return: An :class:`util.RPY` tuple
        """
        gimbal_attitude = self._gimbal_attitude()
        if gimbal_attitude is None:
            self.get_logger().warn('Gimbal attitude not available, cannot return RPY.')
            return None
        assert hasattr(gimbal_attitude, 'q'), 'Gimbal attitude quaternion not available - cannot compute RPY.'

        roll_index = self._roll_index()
        assert roll_index != -1, 'Could not identify roll index in gimbal attitude, cannot return RPY.'

        pitch_index = self._pitch_index()
        assert pitch_index != -1, 'Could not identify pitch index in gimbal attitude, cannot return RPY.'

        yaw_index = self._yaw_index()
        assert yaw_index != -1, 'Could not identify yaw index in gimbal attitude, cannot return RPY.'

        gimbal_euler = Rotation.from_quat(gimbal_attitude.q).as_euler(self.EULER_SEQUENCE, degrees=True)
        if self._vehicle_local_position is None:
            self.get_logger().warn('VehicleLocalPosition is unknown, cannot get heading. Cannot return RPY.')
            return None

        heading = self._vehicle_local_position.heading
        heading = math.degrees(heading)
        assert -180 <= heading <= 180, f'Unexpected heading value: {heading} degrees ([-180, 180] expected).'
        gimbal_yaw = gimbal_euler[yaw_index]
        assert -180 <= gimbal_yaw <= 180, f'Unexpected gimbal yaw value: {gimbal_yaw} ([-180, 180] expected).'

        pitch = -(90 + gimbal_euler[pitch_index])  # TODO: ensure abs(pitch) <= 90?
        if pitch < 0:
            # Gimbal pitch and yaw flip over when abs(gimbal_yaw) should go over 90, adjust accordingly
            assert self.EULER_SEQUENCE == 'YXZ'  # Tested with 'YXZ' this configuration
            gimbal_yaw = 180 - gimbal_yaw
            pitch = 180 + pitch

        self.get_logger().debug('Assuming stabilized gimbal - ignoring vehicle intrinsic pitch and roll for camera RPY.')
        self.get_logger().debug('Assuming zero roll for camera RPY.')  # TODO remove zero roll assumption

        yaw = heading + gimbal_yaw
        yaw = yaw % 360
        if abs(yaw) > 180:  # Important: >, not >= (because we are using mod 180 operation below)
            yaw = yaw % 180 if yaw < 0 else yaw % -180  # Make the compound yaw between -180 and 180 degrees
        roll = 0  # TODO remove zero roll assumption
        #roll = gimbal_euler[roll_index] - 180
        #print(f'roll {roll}')
        rpy = RPY(roll, pitch, yaw)
        #print(f'camera rpy {rpy}')  # TODO: should do yaw = 180-yaw ?

        return rpy

    def _quat_to_rpy(self, q: np.ndarray) -> RPY:
        """Converts the attitude quaternion to roll, pitch, yaw degrees

        See also :meth:`~_rpy_to_quat` for a reverse transformation.

        :param q: Attitude quaternion np.ndarray (shape (4,))
        :return: RPY tuple in degrees
        """
        assert_type(np.ndarray, q)
        assert_shape(q, (4,))
        assert self.EULER_SEQUENCE_VEHICLE == 'xyz'
        euler = Rotation.from_quat(q).as_euler(self.EULER_SEQUENCE_VEHICLE, degrees=True)

        # Fix axes # TODO: do not use hard coded indices
        assert self.EULER_SEQUENCE_VEHICLE == 'xyz'
        roll = euler[2]
        pitch = -euler[1]
        yaw = 180 - euler[0]  # North should be 0

        # TODO: should these be inclusive of upper bound?
        assert -180 <= roll <= 180
        assert -90 <= pitch <= 90
        assert 0 <= yaw <= 360
        rpy = RPY(roll, pitch, yaw)

        return rpy

    def _rpy_to_quat(self, rpy: RPY) -> np.ndarray:
        """Converts roll, pitch, yaw tuple in degrees back to an attitude quaternion.

        This method reverses the axes translations done in :meth:`~_quat_to_rpy`.

        :param rpy: Roll, pitch and yaw in degrees
        :return: Attitude quaternion
        """
        assert_type(RPY, rpy)

        # Reverse axes transformations
        roll = rpy.roll
        pitch = -rpy.pitch
        yaw = 180 - rpy.yaw

        # Reverse the index order
        euler = [yaw, pitch, roll]

        assert self.EULER_SEQUENCE_VEHICLE == 'xyz'
        q = Rotation.from_euler(self.EULER_SEQUENCE_VEHICLE, euler, degrees=True).as_quat()
        q = np.array(q)
        assert_shape(q, (4,))

        return q

    # TODO: try to refactor together with _camera_rpy!
    def _get_vehicle_rpy(self):
        """Returns vehicle roll, pitch and yaw.

        Yaw origin is North.

        :return: Vehicle RPY tuple in degrees
        """
        vehicle_attitude = self._vehicle_attitude
        if vehicle_attitude is None:
            self.get_logger().warn('Vehicle attitude not available, cannot return RPY.')
            return None
        assert hasattr(vehicle_attitude, 'q'), 'Vehicle attitude quaternion not available - cannot compute RPY.'
        rpy = self._quat_to_rpy(vehicle_attitude.q)

        #print(f'vehicle rpy {rpy}')

        return rpy

    def _camera_normal(self) -> Optional[np.ndarray]:
        """Returns camera normal unit vector.

        The camera normal information is needed to determine which solution of :func:`cv2.decomposeHomography` is
        correct. The homography decomposition is done inside :meth:`~_find_and_decompose_homography` as part of
        :meth:`~_process_matches`.

        :return: Camera normal unit vector, or None if not available
        """
        nadir = np.array([0, 0, 1])
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Could not get RPY - cannot compute camera normal.')
            return None
        assert_type(RPY, rpy)

        r = Rotation.from_euler(self.EULER_SEQUENCE, list(rpy), degrees=True)
        camera_normal = r.apply(nadir)
        assert_shape(camera_normal, nadir.shape)

        camera_normal_length = np.linalg.norm(camera_normal)
        # TODO: is this arbitrary check needed?
        if abs(camera_normal_length - 1) >= 0.001:
            self.get_logger().warn(f'Camera normal length: {camera_normal_length}, does not look like a unit vector?.')

        return camera_normal

    def _roll_index(self) -> int:
        """Returns the roll index for used euler vectors.

        Index is determined by the :py:attr:`~EULER_SEQUENCE` constant.

        :return: Roll index
        """
        return self.EULER_SEQUENCE.lower().find('z')

    def _pitch_index(self) -> int:
        """Returns the pitch index for used euler vectors.

        Index is determined by the :py:attr:`~EULER_SEQUENCE` constant.

        :return: Pitch index
        """
        return self.EULER_SEQUENCE.lower().find('y')

    def _yaw_index(self) -> int:
        """Returns the yaw index for used euler vectors.

        Index is determined by the :py:attr:`~EULER_SEQUENCE` constant.

        :return: Yaw index
        """
        return self.EULER_SEQUENCE.lower().find('x')

    def camera_info_callback(self, msg: CameraInfo) -> None:
        """Handles latest camera info message.

        :param msg: CameraInfo message from the PX4-ROS 2 bridge
        :return:
        """
        self.get_logger().debug(f'Camera info received:\n{msg}.')
        self._camera_info = msg
        camera_info_topic = self._topics.get(self.SUBSCRIBE_KEY, {}).get('camera_info', None)
        if camera_info_topic is not None:
            self.get_logger().warn('Assuming camera_info is static - destroying the subscription.')
            camera_info_topic.destroy()

    def vehiclelocalposition_pubsubtopic_callback(self, msg: VehicleLocalPosition) -> None:
        """Handles latest VehicleLocalPosition message.

        Uses the EKF2 system time in the message to synchronize local system time.

        :param msg: VehicleLocalPosition from the PX4-ROS 2 bridge
        :return:
        """
        assert_type(int, msg.timestamp)
        self._vehicle_local_position = msg
        self._sync_timestamps(self._vehicle_local_position.timestamp)


    def _get_dynamic_map_radius(self, altitude: Union[int, float]) -> int:
        """Returns map radius that adjusts for camera altitude.

        :param altitude: Altitude of camera in meters
        :return: Suitable map radius in meters
        """
        assert_type(get_args(Union[int, float]), altitude)
        max_map_radius = self.get_parameter('map_update.max_map_radius').get_parameter_value().integer_value

        camera_info = self._camera_info
        if camera_info is not None:
            assert hasattr(camera_info, 'k')
            assert hasattr(camera_info, 'width')
            w = camera_info.width
            f = camera_info.k[0]
            assert camera_info.k[0] == camera_info.k[4]  # Assert assumption that fx = fy
            hfov = 2 * math.atan(w / (2 * f))
            map_radius = 1.5*hfov*altitude  # Arbitrary padding of 50%
        else:
            # TODO: does this happen? Trying to update map before camera info has been received?
            self.get_logger().warn(f'Could not get camera info, using best guess for map width.')
            map_radius = 3*altitude  # Arbitrary guess

        if map_radius > max_map_radius:
            self.get_logger().warn(f'Dynamic map radius {map_radius} exceeds max map radius {max_map_radius}, using '
                                   f'max_map_radius instead.')
            map_radius = max_map_radius

        return map_radius

    def vehicleglobalposition_pubsubtopic_callback(self, msg: VehicleGlobalPosition) -> None:
        """Handles latest VehicleGlobalPosition message.

        :param msg: VehicleGlobalPosition from the PX4-ROS 2 bridge
        :return:
        """
        self._vehicle_global_position = msg

    def _wms_results_pending(self) -> bool:
        """Checks whether there are pending wms_results.

        :return: True if there are pending results.
        """
        if self._wms_results is not None:
            if not self._wms_results.ready():
                # Previous request still running
                return True

        return False

    def _previous_map_frame_too_close(self, center: Union[LatLon, LatLonAlt], radius: Union[int, float]) -> bool:
        """Checks if previous map frame is too close to new requested one.

        This check is made to avoid retrieving a new map that is almost the same as the previous map. Increasing map
        update interval should not improve accuracy of position estimation unless the map is so old that the field of
        view either no longer completely fits inside (vehicle has moved away or camera is looking in other direction)
        or is too small compared to the size of the map (vehicle altitude has significantly decreased).

        :param center: WGS84 coordinates of new map candidate center
        :param radius: Radius in meters of new map candidate
        :return: True if previous map frame is too close.
        """
        assert_type(get_args(Union[int, float]), radius)
        assert_type(get_args(Union[LatLon, LatLonAlt]), center)
        if self._previous_map_frame is not None:
            if not (abs(self._distance(center, self._previous_map_frame.center)) >
                    self.get_parameter('map_update.update_map_center_threshold').get_parameter_value().integer_value or
                    abs(radius - self._previous_map_frame.radius) >
                    self.get_parameter('map_update.update_map_radius_threshold').get_parameter_value().integer_value):
                return True

        return False

    def _should_update_map(self, center: Union[LatLon, LatLonAlt], radius: Union[int, float]) -> bool:
        """Checks if a new WMS map request should be made to update old map.

        Map is updated unless (1) there is a previous map frame that is close enough to provided center and has radius
        that is close enough to new request, (2) previous WMS request is still processing, or (3) camera pitch is too
        large and gimbal projection is used so that map center would be too far or even beyond the horizon.

        :param center: WGS84 coordinates of new map candidate center
        :param radius: Radius in meters of new map candidate
        :return: True if map should be updated
        """
        assert_type(get_args(Union[int, float]), radius)
        assert_type(get_args(Union[LatLon, LatLonAlt]), center)

        # Check conditions (1) and (2) - previous results pending or requested new map too close to old one
        if self._wms_results_pending() or self._previous_map_frame_too_close(center, radius):
            return False

        # Check condition (3) - whether camera pitch is too large if using gimbal projection
        # TODO: do not even attempt to compute center arg in this case? Would have to be checked earlier?
        use_gimbal_projection = self.get_parameter('map_update.gimbal_projection').get_parameter_value().bool_value
        if use_gimbal_projection:
            max_pitch = self.get_parameter('map_update.max_pitch').get_parameter_value().integer_value
            if self._camera_pitch_too_high(max_pitch):
                self.get_logger().warn(f'Camera pitch not available or above maximum {max_pitch}. Will not update map.')
                return False

        return True

    def gimbaldeviceattitudestatus_pubsubtopic_callback(self, msg: GimbalDeviceAttitudeStatus) -> None:
        """Handles latest GimbalDeviceAttitudeStatus message.

        :param msg: GimbalDeviceAttitudeStatus from the PX4-ROS 2 bridge
        :return:
        """
        self._gimbal_device_attitude_status = msg

    def gimbaldevicesetattitude_pubsubtopic_callback(self, msg: GimbalDeviceSetAttitude) -> None:
        """Handles latest GimbalDeviceSetAttitude message.

        :param msg: GimbalDeviceSetAttitude from the PX4-ROS 2 bridge
        :return:
        """
        """Handles latest GimbalDeviceSetAttitude message."""
        self._gimbal_device_set_attitude = msg

    def vehicleattitude_pubsubtopic_callback(self, msg: VehicleAttitude) -> None:
        """Handles latest VehicleAttitude message.

        :param msg: VehicleAttitude from the PX4-ROS 2 bridge
        :return:
        """
        self._vehicle_attitude = msg

    def _create_vehicle_visual_odometry_msg(self, timestamp: int, position: tuple, rotation: np.ndarray,
                                            pose_covariances: tuple) -> None:
        """Creates a :class:`px4_msgs.msg.VehicleVisualOdometry` message and saves it to
        :py:attr:`~_vehicle_visual_odometry`.

        The :py:attr:`~_vehicle_visual_odometry` value is periodically accessed by :meth:`~_publish_timer_callback` to
        publish the message over the PX4-ROS 2 bridge back to the EKF2 filter.

        See https://docs.px4.io/v1.12/en/advanced_config/tuning_the_ecl_ekf.html#external-vision-system for supported
        EKF2_AID_MASK values when using an external vision system.

        :param timestamp: Timestamp to be included in the outgoing message
        :param position: Position tuple (x, y, z) to be published
        :param rotation: Rotation quaternion to be published (np.ndarray of shape (4,))
        :param pose_covariances: Pose cross-covariances matrix to be published (length = 21)
        :return:
        """
        assert_type(int, timestamp)
        assert_type(tuple, position)
        assert_type(np.ndarray, rotation)
        assert_type(tuple, pose_covariances)
        assert_len(position, 3)
        assert_shape(rotation, (4,))
        assert_len(pose_covariances, 21)
        assert VehicleVisualOdometry is not None, 'VehicleVisualOdometry definition not found (was None).'
        msg = VehicleVisualOdometry()

        if __debug__:
            if self._vehicle_visual_odometry is not None:
                # Should not be create message that is older than previous message that may already have been published
                assert timestamp > self._vehicle_visual_odometry.timestamp

        # Timestamp
        msg.timestamp = timestamp  # now
        msg.timestamp_sample = timestamp  # now  # uint64

        # Position and linear velocity local frame of reference
        msg.local_frame = self.LOCAL_FRAME_NED  # uint8

        # Position
        if position is not None:
            assert len(
                position) == 3, f'Unexpected length for position estimate: {len(position)} (3 expected).'  # TODO: can also be length 2 if altitude is not published, handle that
            assert all(isinstance(x, float) for x in position), f'Position contained non-float elements.'
            msg.x, msg.y, msg.z = position  # float32 North, East, Down
        else:
            self.get_logger().warn('Position tuple was None - publishing NaN as position.')
            msg.x, msg.y, msg.z = (float('nan'),) * 3  # float32 North, East, Down

        # Attitude quaternions
        # Rotation is currently computed with assumed NED frame so it is asserted here just in case
        assert msg.local_frame is self.LOCAL_FRAME_NED, f'Published rotation logic requires that NED frame is used.'
        if rotation is not None:
            msg.q = np.float32(rotation)
            msg.q_offset = (0.0, ) * 4
        else:
            msg.q = (float('nan'),) * 4  # float32
            msg.q_offset = (float('nan'),) * 4

        # Pose covariance matrices
        msg.pose_covariance = pose_covariances

        # Velocity frame of reference
        msg.velocity_frame = self.LOCAL_FRAME_NED  # uint8

        # Velocity
        msg.vx, msg.vy, msg.vz = (float('nan'),) * 3  # float32 North, East, Down

        # Angular velocity - not used
        msg.rollspeed, msg.pitchspeed, msg.yawspeed = (float('nan'),) * 3  # float32
        msg.velocity_covariance = (float('nan'),) * 21  # float32 North, East, Down

        self.get_logger().debug(f'Setting outgoing vehicle visual odometry message as:\n{msg}.')
        self._vehicle_visual_odometry = msg

    # TODO: need to return real! cmaera pitch, not set pitch
    def _camera_pitch(self) -> Optional[Union[int, float]]:
        """Returns camera pitch in degrees relative to nadir.

        Pitch of 0 degrees is a nadir facing camera, while a positive pitch of 90 degrees means the camera is facing
        the direction the vehicle is heading (facing horizon).

        :return: Camera pitch in degrees, or None if not available
        """
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Gimbal RPY not available, cannot compute camera pitch.')
            return None
        assert_type(RPY, rpy)
        return rpy.pitch

    def _gimbal_attitude(self) -> Optional[Union[GimbalDeviceAttitudeStatus, GimbalDeviceSetAttitude]]:
        """Returns 1. GimbalDeviceAttitudeStatus, or 2. GimbalDeviceSetAttitude if 1. is not available.

        NOTE: Gimbal is assumed stabilized but in some instances GimbalDeviceSetAttitude does not reflect what real
        attitude. This may happen for example when vehicle is hovering still and suddenly takes off in some direction,
        there's a sudden yank on the gimbal.

        :return: GimbalDeviceAttitudeStatus or GimbalDeviceSetAttitude message
        """
        gimbal_attitude = self._gimbal_device_attitude_status
        if gimbal_attitude is None:
            self.get_logger().debug('GimbalDeviceAttitudeStatus not available. Trying GimbalDeviceSetAttitude instead.')
            gimbal_attitude = self._gimbal_device_set_attitude
            if gimbal_attitude is None:
                self.get_logger().debug('GimbalDeviceSetAttitude not available. Gimbal attitude status not available.')
        return gimbal_attitude

    @staticmethod
    def _find_and_decompose_homography(mkp_img: np.ndarray, mkp_map: np.ndarray, k: np.ndarray,
                                       camera_normal: np.ndarray, reproj_threshold: float = 1.0,
                                       restrict_affine: bool = False) -> Tuple[np.ndarray, np.ndarray, np.ndarray,
                                                                               np.ndarray]:
        """Processes matching keypoints from img and map and returns homography matrix, mask, translation and rotation.

        Depending on whether :param restrict_affine: applies, either :func:`cv2.findHomography` or
        :func:`cv2.estimateAffinePartial2D` is used for homography estimation. Homography matrix is decompose using
        :func:`cv2.decomposeHomographyMat` to extract camera translation and rotation information, i.e. to determine
        camera position.

        :param mkp_img: Matching keypoints from image
        :param mkp_map: Matching keypoints from map
        :param k: Camera intrinsics matrix
        :param camera_normal: Camera normal unit vector
        :param reproj_threshold: RANSAC reprojection threshold parameter
        :param restrict_affine: Flag indicating whether homography should be restricted to 2D affine transformation
        :return: Tuple containing homography matrix, RANSAC inlier mask, translation and rotation
        """
        min_points = 4  # TODO: use self.MINIMIUM_MATCHES?
        assert_type(np.ndarray, mkp_img)
        assert_type(np.ndarray, mkp_map)
        assert len(mkp_img) >= min_points and len(mkp_map) >= min_points, 'Four points needed to estimate homography.'

        assert_type(bool, restrict_affine)
        assert_type(float, reproj_threshold)
        if not restrict_affine:
            h, h_mask = cv2.findHomography(mkp_img, mkp_map, cv2.RANSAC, reproj_threshold)
        else:
            h, h_mask = cv2.estimateAffinePartial2D(mkp_img, mkp_map)
            h = np.vstack((h, np.array([0, 0, 1])))  # Make it into a homography matrix

        assert_type(np.ndarray, k)
        assert_shape(k, (3, 3))
        num, Rs, Ts, Ns = cv2.decomposeHomographyMat(h, k)

        # Get the one where angle between plane normal and inverse of camera normal is smallest
        # Plane is defined by Z=0 and "up" is in the negative direction on the z-axis in this case
        get_angle_partial = partial(get_angle, -camera_normal)
        angles = list(map(get_angle_partial, Ns))
        index_of_smallest_angle = angles.index(min(angles))
        rotation, translation = Rs[index_of_smallest_angle], Ts[index_of_smallest_angle]

        return h, h_mask, translation, rotation

    @staticmethod
    def _find_homography(mkp_img: np.ndarray, mkp_map: np.ndarray, reproj_threshold: float = 1.0,
                         restrict_affine: bool = False) -> Tuple[np.ndarray, np.ndarray]:
        """

        :param mkp_img:
        :param mkp_map:
        :param reproj_threshold:
        :param restrict_affine:
        :return:
        """
        min_points = 4  # TODO: use self.MINIMIUM_MATCHES?
        assert_type(np.ndarray, mkp_img)
        assert_type(np.ndarray, mkp_map)
        assert len(mkp_img) >= min_points and len(mkp_map) >= min_points, 'Four points needed to estimate homography.'

        assert_type(bool, restrict_affine)
        assert_type(float, reproj_threshold)
        if not restrict_affine:
            h, h_mask = cv2.findHomography(mkp_img, mkp_map, cv2.RANSAC, reproj_threshold)
        else:
            h, h_mask = cv2.estimateAffinePartial2D(mkp_img, mkp_map)
            h = np.vstack((h, np.array([0, 0, 1])))  # Make it into a 3x3 homography matrix

        return h, h_mask

    @staticmethod
    def _decompose_homography(h: np.ndarray, k: np.ndarray, camera_normal: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """

        :param h:
        :param k:
        :param camera_normal: a
        :return:
        """
        assert_type(np.ndarray, k)
        assert_shape(k, (3, 3))
        num, Rs, Ts, Ns = cv2.decomposeHomographyMat(h, k)

        print('Decomposition')
        print(Rs)
        print(Ts)
        print(Ns)

        # Get the one where angle between plane normal and inverse of camera normal is smallest
        # Plane is defined by Z=0 and "up" is in the negative direction on the z-axis in this case
        get_angle_partial = partial(get_angle, -camera_normal)
        angles = list(map(get_angle_partial, Ns))
        print(angles)
        index_of_smallest_angle = angles.index(min(angles))
        print(index_of_smallest_angle)
        rotation, translation = Rs[index_of_smallest_angle], Ts[index_of_smallest_angle]

        return translation, rotation

    def _match_inputs(self, image_frame: ImageFrame) -> dict:
        """Returns a dictionary snapshot of the input data required to perform and process a match.

        Processing of matches is asynchronous, so this method provides a way of taking a snapshot of the input arguments
        to :meth:`_process_matches` from the time image used for the matching was taken.

        The dictionary has the following data:
            map_frame - np.ndarray map_frame to match
            k - np.ndarray Camera intrinsics matrix of shape (3x3) from CameraInfo
            camera_yaw - float Camera yaw in radians
            gimbal_set_attitude - Rotation Gimbal set attitude
            vehicle_attitude - Rotation Vehicle attitude
            map_dim_with_padding - Dim map dimensions including padding for rotation
            img_dim - Dim image dimensions
            local_frame_origin_position - LatLonAlt origin of local frame global frame WGS84
            map_cropped - np.ndarray Rotated and cropped map raster from map_frame.image

        :param image_frame: The image frame from the drone video
        :return: Dictionary with matching input data (give as **kwargs to _process_matches)
        """
        data = {
            'image_frame': image_frame,
            'map_frame': self._map_frame,
            'k': self._camera_info.k.reshape((3, 3)),
            'camera_yaw': math.radians(self._camera_yaw()),  # TODO: refactor internal APIs to use radians to get rid of back and forth conversions
            'gimbal_set_attitude': self._get_gimbal_set_attitude(),
            'vehicle_attitude': self._get_vehicle_attitude(),
            'map_dim_with_padding': self._map_dim_with_padding(),
            'img_dim': self._img_dim(),
            'local_frame_origin_position': (self._vehicle_local_position_ref_latlonalt())
        }

        # Get cropped and rotated map
        camera_yaw = data.get('camera_yaw', None)
        map_frame = data.get('map_frame', None)
        img_dim = data.get('img_dim', None)
        if all((camera_yaw, map_frame, img_dim)):
            assert hasattr(map_frame, 'image'), 'Map frame unexpectedly did not contain the image data.'
            assert -180 <= camera_yaw <= 180, f'Unexpected gimbal yaw value: {camera_yaw} ([-180, 180] expected).'
            data['map_cropped'] = rotate_and_crop_map(map_frame.image, camera_yaw, img_dim)
        else:
            data['map_cropped'] = None

        return data

    def _compute_camera_position(self, t: np.ndarray, center: LatLon, scaling: float) -> LatLon:
        """Returns camera position based on translation vector and map raster center coordinates.

        NOTE:
        The map center coordinates are the coordinates of the center of map_cropped. The cropping and rotation of
        map_cropped is implemented so that its center coordinates match the uncropped, unrotated map's center
        coordinates. Input arg center can therefore be given as the center of the original map's bounding box as long
        as the cropping and rotation implementation remains unchanged.

        :param t: Camera translation vector
        :param center: Map center WGS84 coordinates
        :param scaling: Scaling factor for translation vector (i.e. positive altitude in meters)
        :return: WGS84 coordinates of camera
        """
        assert_type(np.ndarray, t)
        assert_shape(t, (2,))
        azmth = self._get_azimuth(t[0], t[1])
        dist = math.sqrt(t[0] ** 2 + t[1] ** 2)
        scaled_dist = scaling*dist
        assert scaling > 0
        position = self._move_distance(center, (-azmth, scaled_dist))  # Invert azimuth, going the other way
        return position

    def _estimate_camera_distance_to_c(self, fov_wgs84: np.ndarray, c_wgs84: np.ndarray, yaw: float, fx: float,
                                       img_dim: Dim) -> Optional[float]:
        """Returns estimated camera distance to principal point on ground in meters

        Uses triangle similarity where the object of known distance is the line on ground inside the FOV that passes
        through the principal point and is perpendicular to camera optical axis.

        The computation is made a bit more complicated by possibility of roll in gimbal (e.g. when vehicle suddenly
        starts moving, gimbal takes time to stabilize and it may have roll for a while). Roll is not directly used in
        the computation, however, that information is included in the field of view.

        :param fov_wgs84: Field of view corners in WGS84 coordinates
        :param c_wgs84: Principal point projected on ground in WGS84 coordinates
        :param yaw: Gimbal yaw in radians
        :param fx: Camera focal length (in width dimension)
        :param img_dim: Image dimensions
        :return: Distance to principal point on surface in meters
        """
        assert_type(np.ndarray, fov_wgs84)
        assert_type(np.ndarray, c_wgs84)
        assert_type(float, fx)
        assert_type(float, yaw)
        assert_type(Dim, img_dim)
        self.get_logger().info(f'Gimbal yaw used for estimating distance to c: {yaw}.')

        # Estimate intersection between FOV and ground line that is perpendicular to camera optical axis
        fov_shapely_poly = shapely.geometry.Polygon(fov_wgs84.tolist())
        very_large_distance = 100e3  # Meters, shapely does not support lines of infinite length
        c_shapely_line = shapely.geometry.LineString([self._move_distance(c, (yaw+np.pi/2, very_large_distance)),
                                                      self._move_distance(c, (yaw-np.pi/2, very_large_distance))])
        fov_c_line_intersection = list(fov_shapely_poly.intersection(c_shapely_line).coords)
        self.get_logger().info(f'Fov and c line intersection: {fov_c_line_intersection}.')
        if len(fov_c_line_intersection) != 2:
            self.get_logger().info(f'Intersection between FOV and camera optical axis perpendicular line passing '
                                   f'through c was not 2 {len(fov_c_line_intersection)}. Cannot estimate camera '
                                   f'distance to projected principal point on ground (cannot estimate scale).')

        # Length of the line inside FOV that passes through c is perpendicular to camera optical axis
        fov_center_line_meter_length = self._distance(LatLon(*fov_c_line_intersection[0]),
                                                LatLon(*fov_c_line_intersection[1]))
        fov_center_line_pixel_length = 0  # TODO
        assert fov_center_line_meter_length > 0

        # TODO: use fx or fy? if roll high, fy might be more appropriate?

        camera_distance = fov_center_line_meter_length * fx / fov_center_line_pixel_length
        assert_type(float, camera_distance)
        return camera_distance

    def _compute_camera_altitude(self, camera_distance: float, camera_pitch: Union[int, float]) -> Optional[float]:
        """Computes camera altitude in meters (positive) based on distance to principal point and pitch in degrees.

        :param camera_distance: Camera distance to projected principal point
        :param camera_pitch: Camera pitch in degrees
        :return:
        """
        if camera_pitch >= 0:
            self.get_logger().warn(f'Camera pitch {camera_pitch} is positive (not facing ground). Cannot compute '
                                   f'camera altitude from distance.')
            return None

        if abs(camera_pitch) > 90:
            self.get_logger().error(f'Absolute camera pitch {camera_pitch} is unexpectedly higher than 90 degrees. '
                                    f'Cannot compute camera altitude from distance.')
            return None

        if camera_distance < 0:
            self.get_logger().error(f'Camera distance {camera_distance} is unexpectedly negative. '
                                    f'Cannot compute camera altitude from distance.')
            return None

        map_update_max_pitch = self.get_parameter('map_update.max_pitch').get_parameter_value().integer_value
        match_max_pitch = self.get_parameter('misc.max_pitch').get_parameter_value().integer_value
        camera_pitch_from_nadir = 90 + camera_pitch
        if camera_pitch_from_nadir > map_update_max_pitch or camera_pitch_from_nadir > match_max_pitch:
            self.get_logger().warn(f'Camera pitch from nadir {camera_pitch_from_nadir} is higher than one of max pitch '
                                   f'limits (map_update.max_pitch: {map_update_max_pitch}, misc.max_pitch). Are you '
                                   f'sure you want to compute camera distance to principal point projected to ground?.')

        camera_altitude = np.cos(np.radians(-camera_pitch)) * camera_distance  # Negate pitch to get positive altitude
        return camera_altitude

    # TODO: this is "set" camera pitch, not real cmaera pitch so will not work in all cases
    def _camera_pitch_too_high(self, max_pitch: Union[int, float]) -> bool:
        """Returns True if camera pitch exceeds given limit.

        Used to determine whether camera is looking too high up from the ground to make matching against a map
        worthwhile.

        :param max_pitch: The limit for the pitch over which it will be considered too high
        :return: True if pitch is too high
        """
        assert_type(get_args(Union[int, float]), max_pitch)
        camera_pitch = self._camera_pitch()
        if camera_pitch is not None:
            if abs(camera_pitch) > max_pitch:
                self.get_logger().debug(f'Camera pitch {camera_pitch} is above limit {max_pitch}.')
                return True
            if camera_pitch < 0:
                self.get_logger().warn(f'Camera pitch {camera_pitch} is negative.')
        else:
            self.get_logger().warn(f'Could not determine camera pitch.')
            return True

        return False

    def _should_match(self) -> bool:
        """Determines whether _match should be called based on whether previous match is still being processed.

        Match should be attempted if (1) there are no pending match results, and (2) camera pitch is not too high (e.g.
        facing horizon instead of nadir).

        :return: True if matching should be attempted
        """
        # Check condition (1) - that a request is not already running
        if not (self._superglue_results is None or self._superglue_results.ready()):  # TODO: handle timeouts, failures for _superglue_results
            return False

        # Check condition (2) - whether camera pitch is too large
        max_pitch = self.get_parameter('misc.max_pitch').get_parameter_value().integer_value
        if self._camera_pitch_too_high(max_pitch):
            self.get_logger().warn(f'Camera pitch is not available or above limit {max_pitch}. Skipping matching.')
            return False

        return True

    def superglue_worker_error_callback(self, e: BaseException) -> None:
        """Error callback for SuperGlue worker.

        :return:
        """
        self.get_logger().error(f'SuperGlue process returned and error:\n{e}\n{traceback.print_exc()}')

    def superglue_worker_callback(self, results) -> None:
        """Callback for SuperGlue worker.

        Retrieves latest :py:attr:`~_stored_inputs` and uses them to call :meth:`~_process_matches`. The stored inputs
        are needed so that the post-processing is done using the same state information that was used for initiating
        the match in the first place. For example, camera pitch may have changed since then (e.g. if match takes 100ms)
        and current camera pitch should therefore not be used for processing the matches.

        :return:
        """
        mkp_img, mkp_map = results[0]
        assert_len(mkp_img, len(mkp_map))

        visualize_homography_ = self.get_parameter('misc.visualize_homography').get_parameter_value().bool_value
        if not visualize_homography_:
            # _process_matches will not visualize homography if map_cropped is None
            self._stored_inputs['map_cropped'] = None
        self._process_matches(mkp_img, mkp_map, **self._stored_inputs)

    def _compute_local_frame_origin(self, position: Union[LatLon, LatLonAlt]):
        """Computes local frame global coordinates from local frame coordinates and their known global coordinates.

        VehicleLocalPosition does not include the reference coordinates for the local frame if e.g. GPS is turned off.
        This function computes the reference coordinates by translating the inverse of current local coordinates from
        current estimated global position.

        :param position: WGS84 coordinates of current vehicle local position
        :return:
        """
        assert self._vehicle_local_position is not None
        assert np.isnan(self._vehicle_local_position.ref_lat), \
            f'_vehicle_local_position.ref_lat was {self._vehicle_local_position.ref_lat}. You should not try to ' \
            f'compute the local origin unless it is not provided in VehicleLocalPosition'
        assert np.isnan(self._vehicle_local_position.ref_lon), \
            f'_vehicle_local_position.ref_lon was {self._vehicle_local_position.ref_lon}. You should not try to ' \
            f'compute the local origin unless it is not provided in VehicleLocalPosition'
        assert self._local_origin is None, f'self._local_origin was {self.__local_origin}. You should not try to ' \
                                           f'recompute the local origin if it has already been done.'
        x, y = -self._vehicle_local_position.x, -self._vehicle_local_position.y
        assert_type(float, x)
        assert_type(float, y)
        azmth = self._get_azimuth(x, y)
        dist = math.sqrt(x ** 2 + y ** 2)
        local_origin = LatLonAlt(*(self._move_distance(position, (azmth, dist)) + (0,)))
        self.get_logger().info(f'Local frame origin set at {local_origin}, this should happen only once.')
        return local_origin

    def _compute_local_frame_position(self, position: LatLonAlt, origin: LatLonAlt) -> Optional[Tuple[float, float, float]]:
        """Computes position of WGS84 coordinates in local frame coordinates

        :param origin: WGS84 coordiantes of local frame origin
        :param position: WGS84 coordinates and altitude in meters of estimated camera position
        :return: Tuple containing x, y and z coordinates (meters) in local frame
        """
        assert_type(LatLonAlt, position)
        assert_type(LatLonAlt, origin)

        lats_orig = (origin.lat, origin.lat)
        lons_orig = (origin.lon, origin.lon)
        lats_term = (origin.lat, position.lat)
        lons_term = (position.lon, origin.lon)
        _, __, dist = self._geod.inv(lons_orig, lats_orig, lons_term, lats_term)

        lat_diff = math.copysign(dist[1], position.lat - origin.lat)
        lon_diff = math.copysign(dist[0], position.lon - origin.lon)

        alt = position.alt - origin.alt
        if alt < 0:
            self.get_logger().warn(f'Computed altitude {alt} was negative. Cannot compute local frame position.')
            return None

        return lat_diff, lon_diff, -alt

    def _compute_attitude_quaternion(self, ll: np.ndarray, lr: np.ndarray, vehicle_rpy: RPY) -> Optional[Tuple[float]]:
        """Computes attitude quaternion against NED frame for outgoing VehicleVisualOdometry message.

        Attitude estimate adjusts vehicle heading (used to rotate the map raster together with gimbal yaw) by the angle
        of the estimated field of view bottom side to the x-axis vector. This adjustment angle is expected to be very
        small in most cases as the FOV should align well with the rotated map raster.

        :param ll: Lower left corner pixel coordinates of estimated field of view
        :param lr: Lower right corner pixel coordinates of estimated field of view
        :return: Quaternion tuple, or None if attitude information is not available
        """
        # TODO: Seems like when gimbal (camera) is nadir facing, PX4 automatically rotates it back to face vehicle
        #  heading. However, GimbalDeviceSetAttitude stays the same so this will give an estimate for vehicle heading
        #  that is off by camera_yaw in this case. No problems with nadir-facing camera only if there is no gimbal yaw.
        #  Need to log a warning or even an error in these cases!
        assert_type(np.ndarray, ll)
        assert_type(np.ndarray, lr)
        if self._vehicle_attitude is not None:
            fov_adjustment_angle = math.degrees(get_angle(np.float32([1, 0]), (lr - ll).squeeze(), normalize=True))
            assert hasattr(self._vehicle_attitude, 'q')
            vehicle_yaw = vehicle_rpy.yaw + fov_adjustment_angle
            quaternion = self._rpy_to_quat(RPY(vehicle_rpy.roll, vehicle_rpy.pitch, vehicle_yaw))
            assert_len(quaternion, 4)
            self.get_logger().debug(f'Heading adjustment angle: {fov_adjustment_angle}.')
            self.get_logger().debug(f'Vehicle yaw: {vehicle_yaw}.')
            return quaternion
        else:
            # TODO: when does this happen? When do we not have this info? Should have it always, at least pitch and roll?
            return None

    def _gimbal_is_stable(self, gimbal_attitude: Rotation) -> bool:
        """Returns True if difference between gimbal and vehilce NED frame attitudes matches gimbal set attitude.

        This is needed because :class:`px4_msgs.msg.GimbalDeviceAttitudeStatus` is assumed to be unknown.

        :param gimbal_attitude: Gimbal attitude in NED frame
        :return: True if gimbal has stabilized (gimbal attitude matches set attitude)
        """
        # TODO: need to make sure gimbal attitude is in same frame of reference as set_attitude (different sources so maybe not!)
        set_attitude = self._gimbal_device_set_compound_attitude()
        if __debug__:
            difference = set_attitude * gimbal_attitude.inv()
            difference_rpy = self._rotation_to_rpy(difference)
            self.get_logger().info(f'Gimbal difference to stable: {difference_rpy}')
        return set_attitude == gimbal_attitude

    def _estimate_gimbal_relative_attitude(self, gimbal_attitude: Rotation) -> Optional[Rotation]:
        """Estimates gimbal attitude in body frame."""
        set_compound_attitude = self._gimbal_device_set_compound_attitude()
        if set_compound_attitude is None:
            self.get_logger().warn(f'Gimbal set compound attitude not available, cannot estimate gimbal attitude.')
            return None
        difference = set_compound_attitude * gimbal_attitude.inv()
        set_attitude = self._get_gimbal_set_attitude()
        if set_compound_attitude is None:
            self.get_logger().warn(f'Gimbal set compound attitude not available, cannot estimate gimbal attitude.')
            return None
        attitude = set_attitude * difference

        return attitude

    def _process_matches(self, mkp_img: np.ndarray, mkp_map: np.ndarray, image_frame: ImageFrame, map_frame: MapFrame,
                         k: np.ndarray, camera_yaw: float, gimbal_set_attitude: Rotation,
                         vehicle_attitude: Rotation, map_dim_with_padding: Dim, img_dim: Dim,
                         local_frame_origin_position: Optional[LatLonAlt], map_cropped: Optional[np.ndarray] = None)\
            -> None:
        """Process the matching image and map keypoints into an outgoing :class:`px4_msgs.msg.VehicleVisualOdometry`
        message.

        Computes vehicle position and attitude in vehicle local frame. The API for this method is designed so that the
        dictionary returned by :meth:`~_match_inputs` can be passed onto this method as keyword arguments (**kwargs).

        :param mkp_img: Matching keypoints in drone image
        :param mkp_map: Matching keypoints in map raster
        :param image_frame: The drone image
        :param map_frame: The map raster
        :param k: Camera intrinsics matrix from CameraInfo from time of match (from _match_inputs)
        :param camera_yaw: Camera yaw in radians from time of match (from _match_inputs)  # Maybe rename map rotation so less confusion with gimbal attitude stuff extractd from rotation matrix?
        :param gimbal_set_attitude: Gimbal set attitude
        :param vehicle_attitude: Vehicle attitude
        :param map_dim_with_padding: Map dimensions with padding from time of match (from _match_inputs)
        :param img_dim: Drone image dimensions from time of match (from _match_inputs)
        :param local_frame_origin_position: Local frame origin coordinates from time of match (from _match_inputs)
        :param map_cropped: Optional map cropped image, visualizes matches if provided

        :return:
        """
        if len(mkp_img) < self.HOMOGRAPHY_MINIMUM_MATCHES:
            self.get_logger().warn(f'Found {len(mkp_img)} matches, {self.HOMOGRAPHY_MINIMUM_MATCHES} required. '
                                   f'Skipping frame.')
            return None

        assert_shape(k, (3, 3))

        # Transforms from rotated and cropped map pixel coordinates to WGS84
        pix_to_wgs84_, unrotated_to_wgs84, uncropped_to_unrotated, pix_to_uncropped = pix_to_wgs84_affine(
            map_dim_with_padding, map_frame.bbox, -camera_yaw, img_dim)

        # Estimate extrinsic and homography matrices
        padding = np.array([[0]]*len(mkp_img))
        mkp_map_3d = np.hstack((mkp_map, padding))  # Set all z-coordinates to zero
        dist_coeffs = np.zeros((4, 1))
        _, r, t, __ = cv2.solvePnPRansac(mkp_map_3d, mkp_img, k, dist_coeffs, iterationsCount=10)
        r, _ = cv2.Rodrigues(r)
        e = np.hstack((r, t))  # Extrinsic matrix (for homography estimation)
        pos = -r.T @ t  # Inverse extrinsic (for computing camera position in object coordinates)
        h = np.linalg.inv(k @ np.delete(e, 2, 1))  # Remove z-column (depth) and multiply by intrinsics, then invert to get homography matrix from img to map
        self.get_logger().info(f'Estimated translation: {t}.')
        self.get_logger().info(f'Estimated position: {pos}.')

        # Field of view in both pixel (rotated and cropped map raster) and WGS84 coordinates
        h_wgs84 = pix_to_wgs84_ @ h
        fov_pix, c_pix = get_fov_and_c(image_frame.image, h)
        fov_wgs84, c_wgs84 = get_fov_and_c(image_frame.image, h_wgs84)
        if map_cropped is not None:
            # Optional visualization of matched keypoints and field of view boundary
            visualize_homography('Keypoint matches and FOV', image_frame.image, map_cropped, mkp_img, mkp_map, fov_pix)
        image_frame.fov = fov_wgs84
        self.get_logger().info(f'Estimated c_pix: {c_pix}.')  # Should be 0,0
        self.get_logger().info(f'Estimated c_wgs84: {c_wgs84}.')

        # Compute altitude scaling:
        # Altitude in t is in rotated and cropped map raster pixel coordinates. We can use fov_pix and fov_wgs84 to
        # find out the right scale in meters. Distance in pixels is computed from lower left and lower right corners
        # of the field of view (bottom of fov assumed more stable than top), while distance in meters is computed from
        # the corresponding WGS84 latitude and latitude coordinates.
        distance_in_pixels = np.linalg.norm(fov_pix[1]-fov_pix[2])  # fov_pix[1]: lower left, fov_pix[2]: lower right
        distance_in_meters = self._distance(LatLon(*fov_wgs84[1].squeeze().tolist()),
                                            LatLon(*fov_wgs84[2].squeeze().tolist()))
        altitude_scaling = abs(distance_in_meters / distance_in_pixels)
        self.get_logger().info(f'Estimated altitude scaling: {altitude_scaling}.')

        # Translation in WGS84
        t_wgs84 = pix_to_wgs84_ @ np.append(pos[0:2], 1)
        t_wgs84[2] = -altitude_scaling * pos[2]  # In NED frame z-coordinate is negative above ground but make altitude positive

        self.get_logger().info(f'Estimated translation in WGS84: {t_wgs84}.')
        image_frame.position = LatLonAlt(*t_wgs84.squeeze().tolist())  # TODO: should just ditch LatLonAlt and keep numpy arrays?
        self.get_logger().info(f'Estimated latlon position: {image_frame.position}.')

        # Check that we have everything we need to publish vehicle_visual_odometry
        if not all(image_frame.position) or any(map(np.isnan, image_frame.position)):
            self.get_logger().debug(f'Could not determine global position. Cannot create vehicle visual odometry '
                                    f'message.')
            return None

        # If no GPS available, local frame origin must be computed (or retrieved from memory if it was computed earlier)
        if local_frame_origin_position is None:
            if self._local_origin is None:
                self._local_origin = self._compute_local_frame_origin(image_frame.position)
            local_frame_origin_position = self._local_origin

        # Compute camera position in local frame
        local_position = self._compute_local_frame_position(image_frame.position, local_frame_origin_position)
        if local_position is None:
            self.get_logger().debug(f'Could not determine local position. Cannot create vehicle visual odometry '
                                    f'message.')
            return None
        self.get_logger().debug(f'Local frame position: {local_position}, origin ref position '
                                f'{local_frame_origin_position}.')

        # Convert estimated rotation to attitude quaternion for publishing
        gimbal_attitude = Rotation.from_matrix(r)  # in rotated map frame
        gimbal_rpy = self._rotation_to_rpy(gimbal_attitude)
        assert -np.pi <= camera_yaw <= np.pi  # should be radians
        quaternion = gimbal_attitude.as_quat()  # TODO: may have to transform axis to make this compatible with EKF2 frame of reference
        self.get_logger().info(f'Visually estimated camera RPY: {gimbal_rpy}.')

        # 8. Estimate vehicle attitude
        # Challenge here is to figure out real gimbal attitude versus vehicle body frame
        # We only know the gimbal set relative attitude but do not know real relative attitude vs. vehicle body
        # This happens typically when there are large sudden changes to vehicle pitch or roll and gimbal has not yet stabilized
        # Adjust gimbal_set_attitude by difference between what is visually seen and what the setting value is
        # The difference can be either gimbal attitude not being at setting value, or vehicle true attitude not being
        # local position message is telling us. Assume local position attitude numbers are accurate, gimbal_difference
        # is due to gimbal not yet being stabilized.
        gimbal_difference = vehicle_attitude * gimbal_set_attitude * gimbal_attitude.inv()
        vehicle_attitude_estimate = gimbal_attitude * (gimbal_set_attitude * gimbal_difference).inv()

        vehicle_estimate_rpy = self._rotation_to_rpy(vehicle_attitude_estimate)

        if vehicle_estimate_rpy is not None:
            # Update covariance data window and check if covariance matrix is available
            # Do not create message if covariance matrix not yet available
            # TODO: move this stuff into dedicated method to declutter _process_matches a bit more
            assert_type(int, image_frame.timestamp)
            vehicle_rpy_radians = tuple(map(lambda x: math.radians(x), vehicle_estimate_rpy))
            self._push_covariance_data(local_position, vehicle_rpy_radians)
            if not self._covariance_window_full():
                self.get_logger().warn('Not enough data to estimate covariances yet, should be working on it, please wait. '
                                       'Skipping creating vehicle_visual_odometry message for now.')
            else:
                covariance = np.cov(self._pose_covariance_data_window, rowvar=False)
                covariance_urt = tuple(covariance[np.triu_indices(6)])  # Transform URT to flat vector of length 21
                assert_len(covariance_urt, 21)
                self._create_vehicle_visual_odometry_msg(image_frame.timestamp, local_position, quaternion, covariance_urt)

        export_geojson = self.get_parameter('misc.export_position').get_parameter_value().string_value
        if export_geojson is not None:
            self._export_position(image_frame.position, image_frame.fov, export_geojson)

    def _export_position(self, position: Union[LatLon, LatLonAlt], fov: np.ndarray, filename: str) -> None:
        """Exports the computed position and field of view (FOV) into a geojson file.

        The GeoJSON file is not used by the node but can be accessed by GIS software to visualize the data it contains.

        :param position: Computed camera position
        :param: fov: Field of view of camera
        :param filename: Name of file to write into
        :return:
        """
        assert_type(get_args(Union[LatLon, LatLonAlt]), position)
        assert_type(np.ndarray, fov)
        assert_type(str, filename)
        point = Feature(geometry=Point((position.lon, position.lat)))  # TODO: add name/description properties
        corners = np.flip(fov.squeeze()).tolist()
        corners = [tuple(x) for x in corners]
        corners = Feature(geometry=Polygon([corners]))  # TODO: add name/description properties
        features = [point, corners]
        feature_collection = FeatureCollection(features)
        try:
            with open(filename, 'w') as f:
                dump(feature_collection, f)
        except Exception as e:
            self.get_logger().error(f'Could not write file {filename} because of exception:'
                                    f'\n{e}\n{traceback.print_exc()}')

    def _match(self, image_frame: ImageFrame, map_cropped: np.ndarray) -> None:
        """Instructs the neural network to match camera image to map image.

        :param image_frame: The image frame to match
        :param map_cropped: Cropped and rotated map raster (aligned with image)
        :return:
        """
        assert self._superglue_results is None or self._superglue_results.ready()
        self._superglue_results = self._superglue_pool.starmap_async(
            self._superglue_pool_worker,
            [(image_frame.image, map_cropped)],
            callback=self.superglue_worker_callback,
            error_callback=self.superglue_worker_error_callback
        )

    def terminate_wms_pool(self):
        """Terminates the WMS Pool.

        :return:
        """
        if self._wms_pool is not None:
            self.get_logger().info('Terminating WMS pool.')
            self._wms_pool.terminate()

    def destroy_timers(self):
        """Destroys the vehicle visual odometry publish and map update timers.

        :return:
        """
        if self._publish_timer is not None:
            self.get_logger().info('Destroying publish timer.')
            assert_type(rclpy.timer.Timer, self._publish_timer)
            self._publish_timer.destroy()

        if self._publish_timer is not None:
            self.get_logger().info('Destroying map update timer.')
            assert_type(rclpy.timer.Timer, self._map_update_timer)
            self._map_update_timer.destroy()


def main(args=None):
    """Starts and terminates the ROS 2 node.

    Also starts cProfile profiling in debugging mode.

    :param args: Any args for initializing the rclpy node
    :return:
    """
    #if __debug__:
    #    pr = cProfile.Profile()  # TODO: re-enable
    #    pr.enable()
    #else:
    pr = None
    try:
        rclpy.init(args=args)
        matcher = MapNavNode('map_nav_node', share_dir, superglue_dir)
        rclpy.spin(matcher)
    except KeyboardInterrupt as e:
        print(f'Keyboard interrupt received:\n{e}')
        if pr is not None:
            # Print out profiling stats
            pr.disable()
            s = io.StringIO()
            ps = pstats.Stats(pr, stream=s).sort_stats(pstats.SortKey.CUMULATIVE)
            ps.print_stats()
            print(s.getvalue())
    finally:
        matcher.destroy_timers()
        matcher.terminate_wms_pool()
        matcher.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
