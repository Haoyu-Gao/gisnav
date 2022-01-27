"""Module that contains the MapNavNode ROS 2 node."""
import sys
import rclpy
import traceback
import math
import numpy as np
import cv2
import time
import importlib
import os
import yaml

from ament_index_python.packages import get_package_share_directory
PACKAGE_NAME = 'python_px4_ros2_map_nav'  # TODO: try to read from somewhere (e.g. package.xml)

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
from geojson import Point, Polygon, Feature, FeatureCollection, dump

from cv_bridge import CvBridge
from scipy.spatial.transform import Rotation
from functools import partial
from python_px4_ros2_map_nav.util import BBox, Dim, visualize_homography, LatLon, \
    TimePair, RPY, LatLonAlt, ImageFrame, MapFrame
from python_px4_ros2_map_nav.transform import fov_center, get_fov_and_c, pix_to_wgs84_affine, rotate_and_crop_map, \
    inv_homography_from_k_and_e
from python_px4_ros2_map_nav.assertions import assert_type, assert_ndim, assert_len, assert_shape
from python_px4_ros2_map_nav.ros_param_defaults import Defaults
from python_px4_ros2_map_nav.keypoint_matchers.keypoint_matcher import KeypointMatcher
from python_px4_ros2_map_nav.wms import WMSClient
from px4_msgs.msg import VehicleAttitude, VehicleLocalPosition, VehicleGlobalPosition, GimbalDeviceAttitudeStatus, \
    GimbalDeviceSetAttitude, VehicleGpsPosition
from sensor_msgs.msg import CameraInfo, Image


class MapNavNode(Node):
    """ROS 2 Node that publishes position estimate based on visual match of drone video to map of same location."""
    # Minimum matches for homography estimation, should be at least 4
    HOMOGRAPHY_MINIMUM_MATCHES = 4

    # Encoding of input video (input to CvBridge)
    IMAGE_ENCODING = 'bgr8'  # E.g. gscam2 only supports bgr8 so this is used to override encoding in image header

    # Ellipsoid model used by pyproj
    PYPROJ_ELLIPSOID = 'WGS84'

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
    VEHICLE_GPS_POSITION_TOPIC_NAME = 'VehicleGpsPosition_PubSubTopic'
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
            TOPIC_NAME_KEY: VEHICLE_GPS_POSITION_TOPIC_NAME,
            CLASS_KEY: VehicleGpsPosition,
            PUBLISH_KEY: True
        }
    ]

    def __init__(self, node_name: str) -> None:
        """Initializes the ROS 2 node.

        :param node_name: Name of the node
        """
        assert_type(node_name, str)
        super().__init__(node_name)
        self.name = node_name

        # Setup config and declare ROS parameters
        self._declare_ros_params()

        # WMS client and requests in a separate process
        self._wms_results = None  # Must check for None when using this
        url = self.get_parameter('wms.url').get_parameter_value().string_value
        version = self.get_parameter('wms.version').get_parameter_value().string_value
        timeout = self.get_parameter('wms.request_timeout').get_parameter_value().integer_value
        assert_type(url, str)
        assert_type(version, str)
        assert_type(timeout, int)
        self._wms_pool = Pool(1, initializer=WMSClient.initializer, initargs=(url, version, timeout))  # Do not increase the process count, it should be 1

        # Setup map update timer
        self._map_update_timer = self._setup_map_update_timer()

        # Dict for storing all microRTPS bridge subscribers and publishers
        self._topics = {self.PUBLISH_KEY: {}, self.SUBSCRIBE_KEY: {}}
        self._setup_topics()

        # Time of publication of mock GPS message  # TODO: currently not used
        self._publish_timestamp = None

        # Converts image_raw to cv2 compatible image
        self._cv_bridge = CvBridge()

        # Setup matching
        self._stored_inputs = None  # Must check for None when using this
        self._matching_results = None  # Must check for None when using this
        class_path = self.get_parameter('matcher.class').get_parameter_value().string_value
        matcher_params_file = self.get_parameter('matcher.params_file').get_parameter_value().string_value
        if class_path is None or matcher_params_file is None:
            msg = f'Class path {class_path} or init args {matcher_params_file} for matcher was None.'
            self.get_logger.error(msg)
            raise ValueError(msg)
        module_name, class_name = class_path.rsplit('.', 1)
        # noinspection PyTypeChecker
        self._kp_matcher = self._import_class(class_name, module_name)
        #assert_type(kp_matcher, KeypointMatcher)  # TODO: seems like it recognizes it as an ABCMeta class
        args = self._load_config(matcher_params_file)['args']

        # Do not increase the process count, it should be 1
        # TODO: need to use torch pool? Torch not needed in general case?
        self._matching_pool = torch.multiprocessing.Pool(1, initializer=self._kp_matcher.initializer, initargs=args)

        # Used for pyproj transformations
        self._geod = Geod(ellps=self.PYPROJ_ELLIPSOID)

        # Must check for None when using these
        # self._image_frame = None  # Not currently used / needed
        self._map_frame = None
        self._previous_map_frame = None
        self._previous_image_frame = None

        # Stored solution for the PnP problem
        self._r = None
        self._t = None

        self._time_sync = None  # For storing local and foreign (EKF2) timestamps

        self._estimation_history = None  # Windowed estimates for computing estimate SD and variance

        # Properties that are mapped to microRTPS bridge topics, must check for None when using them
        self._camera_info = None
        self._vehicle_local_position = None
        self._vehicle_global_position = None
        self._vehicle_attitude = None
        self._gimbal_device_attitude_status = None
        self._gimbal_device_set_attitude = None

    @property
    def name(self) -> str:
        """Node name."""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        assert_type(value, str)
        self._name = value

    @property
    def _r(self) -> Optional[np.ndarray]:
        """Rotation vector, solution to the PnP problem in :meth:`~_process_matches`."""
        return self.__r

    @_r.setter
    def _r(self, value: Optional[np.ndarray]) -> None:
        assert_type(value, get_args(Optional[np.ndarray]))
        self.__r = value

    @property
    def _t(self) -> Optional[np.ndarray]:
        """Translation vector, solution to the PnP problem in :meth:`~_process_matches`."""
        return self.__t

    @_t.setter
    def _t(self, value: Optional[np.ndarray]) -> None:
        assert_type(value, get_args(Optional[np.ndarray]))
        self.__t = value

    @property
    def _kp_matcher(self) -> KeypointMatcher:
        """Dynamically loaded keypoint matcher"""
        return self.__kp_matcher

    @_kp_matcher.setter
    def _kp_matcher(self, value: KeypointMatcher) -> None:
        #assert_type(value, KeypointMatcher)  # TODO: fix this
        self.__kp_matcher = value

    @property
    def _time_sync(self) -> Optional[TimePair]:
        """A :class:`python_px4_ros2_map_nav.util.TimePair` with local and foreign (EKF2) timestamps in microseconds

        The pair will contain the local system time and the EKF2 time received via the PX4-ROS 2 bridge. The pair can
        then at any time be used to locally estimate the EKF2 system time.
        """
        return self.__time_sync

    @_time_sync.setter
    def _time_sync(self, value: Optional[TimePair]) -> None:
        assert_type(value, get_args(Optional[TimePair]))
        self.__time_sync = value

    @property
    def _estimation_history(self) -> Optional[np.ndarray]:
        """Windowed data for estimating position error"""
        return self.__estimation_history

    @_estimation_history.setter
    def _estimation_history(self, value: Optional[np.ndarray]) -> None:
        assert_type(value, get_args(Optional[np.ndarray]))
        self.__estimation_history = value

    @property
    def _wms_pool(self) -> Pool:
        """Web Map Service client for fetching map rasters."""
        return self.__wms_pool

    @_wms_pool.setter
    def _wms_pool(self, value: Pool) -> None:
        assert_type(value, Pool)
        self.__wms_pool = value

    @property
    def _wms_results(self) -> Optional[AsyncResult]:
        """Asynchronous results from a WMS client request."""
        return self.__wms_results

    @_wms_results.setter
    def _wms_results(self, value: Optional[AsyncResult]) -> None:
        assert_type(value, get_args(Optional[AsyncResult]))
        self.__wms_results = value

    @property
    def _map_update_timer(self) -> rclpy.timer.Timer:
        """Timer for throttling map update WMS requests."""
        return self.__map_update_timer

    @_map_update_timer.setter
    def _map_update_timer(self, value: rclpy.timer.Timer) -> None:
        assert_type(value, rclpy.timer.Timer)
        self.__map_update_timer = value

    @property
    def _matching_pool(self) -> torch.multiprocessing.Pool:
        """Pool for running a :class:`~keypoint_matcher.KeypointMatcher` in dedicated process"""
        return self.__matching_pool

    @_matching_pool.setter
    def _matching_pool(self, value: torch.multiprocessing.Pool) -> None:
        # TODO assert type
        #assert_type(torch.multiprocessing.Pool, value)
        self.__matching_pool = value

    @property
    def _stored_inputs(self) -> dict:
        """Inputs stored at time of launching a new asynchronous match that are needed for processing its results.

        See :meth:`~_process_matches` for description of keys and values stored in the dictionary.
        """
        return self.__stored_inputs

    @_stored_inputs.setter
    def _stored_inputs(self, value: Optional[dict]) -> None:
        assert_type(value, get_args(Optional[dict]))
        self.__stored_inputs = value

    @property
    def _matching_results(self) -> Optional[AsyncResult]:
        """Asynchronous results from a matching process."""
        return self.__matching_results

    @_matching_results.setter
    def _matching_results(self, value: Optional[AsyncResult]) -> None:
        assert_type(value, get_args(Optional[AsyncResult]))
        self.__matching_results = value

    @property
    def _publish_timestamp(self) -> Optional[int]:
        """Timestamp in of when last :class:`px4_msgs.msg.VehicleGpsPosition` message was published."""
        return self.__publish_timestamp

    @_publish_timestamp.setter
    def _publish_timestamp(self, value: Optional[int]) -> None:
        assert_type(value, get_args(Optional[int]))
        self.__publish_timestamp = value

    @property
    def _topics(self) -> dict:
        """Dictionary that stores all rclpy publishers and subscribers."""
        return self.__topics

    @_topics.setter
    def _topics(self, value: dict) -> None:
        assert_type(value, dict)
        self.__topics = value

    @property
    def _geod(self) -> Geod:
        """Stored pyproj Geod instance for performing geodetic computations."""
        return self.__geod

    @_geod.setter
    def _geod(self, value: Geod) -> None:
        assert_type(value, Geod)
        self.__geod = value

    @property
    def _map_frame(self) -> Optional[MapFrame]:
        """The map raster from the WMS server response along with supporting metadata."""
        return self.__map_frame

    @_map_frame.setter
    def _map_frame(self, value: Optional[MapFrame]) -> None:
        assert_type(value, get_args(Optional[MapFrame]))
        self.__map_frame = value

    @property
    def _cv_bridge(self) -> CvBridge:
        """CvBridge that decodes incoming PX4-ROS 2 bridge images to cv2 images."""
        return self.__cv_bridge

    @_cv_bridge.setter
    def _cv_bridge(self, value: CvBridge) -> None:
        assert_type(value, CvBridge)
        self.__cv_bridge = value

    @property
    def _previous_map_frame(self) -> Optional[MapFrame]:
        """The previous map frame which is compared to current map frame to determine need for another update."""
        return self.__previous_map_frame

    @_previous_map_frame.setter
    def _previous_map_frame(self, value: Optional[MapFrame]) -> None:
        assert_type(value, get_args(Optional[MapFrame]))
        self.__previous_map_frame = value

    @property
    def _previous_image_frame(self) -> Optional[ImageFrame]:
        """The previous image frame which is needed for computing the velocity estimate."""
        return self.__previous_image_frame

    @_previous_image_frame.setter
    def _previous_image_frame(self, value: Optional[ImageFrame]) -> None:
        assert_type(value, get_args(Optional[ImageFrame]))
        self.__previous_image_frame = value

    @property
    def _camera_info(self) -> Optional[CameraInfo]:
        """CameraInfo received via the PX4-ROS 2 bridge."""
        return self.__camera_info

    @_camera_info.setter
    def _camera_info(self, value: Optional[CameraInfo]) -> None:
        assert_type(value, get_args(Optional[CameraInfo]))
        self.__camera_info = value

    @property
    def _vehicle_local_position(self) -> Optional[VehicleLocalPosition]:
        """VehicleLocalPosition received via the PX4-ROS 2 bridge."""
        return self.__vehicle_local_position

    @_vehicle_local_position.setter
    def _vehicle_local_position(self, value: Optional[VehicleLocalPosition]) -> None:
        assert_type(value, get_args(Optional[VehicleLocalPosition]))
        self.__vehicle_local_position = value

    @property
    def _vehicle_global_position(self) -> Optional[VehicleGlobalPosition]:
        """VehicleGlobalPosition received via the PX4-ROS 2 bridge."""
        return self.__vehicle_global_position

    @_vehicle_global_position.setter
    def _vehicle_global_position(self, value: Optional[VehicleGlobalPosition]) -> None:
        assert_type(value, get_args(Optional[VehicleGlobalPosition]))
        self.__vehicle_global_position = value

    @property
    def _vehicle_attitude(self) -> Optional[VehicleAttitude]:
        """VehicleAttitude received via the PX4-ROS 2 bridge."""
        return self.__vehicle_attitude

    @_vehicle_attitude.setter
    def _vehicle_attitude(self, value: Optional[VehicleAttitude]) -> None:
        assert_type(value, get_args(Optional[VehicleAttitude]))
        self.__vehicle_attitude = value

    @property
    def _gimbal_device_attitude_status(self) -> Optional[GimbalDeviceAttitudeStatus]:
        """GimbalDeviceAttitudeStatus received via the PX4-ROS 2 bridge."""
        return self.__gimbal_device_attitude_status

    @_gimbal_device_attitude_status.setter
    def _gimbal_device_attitude_status(self, value: Optional[GimbalDeviceAttitudeStatus]) -> None:
        assert_type(value, get_args(Optional[GimbalDeviceAttitudeStatus]))
        self.__gimbal_device_attitude_status = value

    @property
    def _gimbal_device_set_attitude(self) -> Optional[GimbalDeviceSetAttitude]:
        """GimbalDeviceSetAttitude received via the PX4-ROS 2 bridge."""
        return self.__gimbal_device_set_attitude

    @_gimbal_device_set_attitude.setter
    def _gimbal_device_set_attitude(self, value: Optional[GimbalDeviceSetAttitude]) -> None:
        assert_type(value, get_args(Optional[GimbalDeviceSetAttitude]))
        self.__gimbal_device_set_attitude = value

    def _load_config(self, yaml_file: str) -> dict:
        """Loads config from the provided YAML file.

        :param yaml_file: Path to the yaml file
        :return: The loaded yaml file as dictionary
        """
        assert_type(yaml_file, str)
        with open(os.path.join(get_package_share_directory(PACKAGE_NAME), yaml_file), 'r') as f:
            try:
                config = yaml.safe_load(f)
                self.get_logger().info(f'Loaded config:\n{config}.')
                return config
            except Exception as e:
                self.get_logger().error(f'Could not load config file {yaml_file} because of exception:'
                                        f'\n{e}\n{traceback.print_exc()}')

    def _variance_window_full(self) -> bool:
        """Returns true if the variance estimation window is full.

        :return: True if :py:attr:`~_estimation_history` is full
        """
        window_length = self.get_parameter('misc.variance_estimation_length').get_parameter_value().integer_value
        obs_count = len(self._estimation_history)
        if self._estimation_history is not None and obs_count == window_length:
            return True
        else:
            assert 0 <= obs_count < window_length
            return False

    def _push_estimates(self, position: np.ndarray, velocity: np.ndarray, speed: np.ndarray, course: np.ndarray) \
            -> None:
        """Pushes position, velocity, speed and course estimates to :py:attr:`~_estimation_history`

        Pops the oldest estimate from the window if needed.

        :param position: Pose translation (x, y, z) in WGS84
        :param position: Velocity (vx, vy, vz) in meters/second
        :param speed: Speed in meters per second (norm of velocity vector)
        :param course: Movement of direction of vehicle in radians in NED frame (not 'heading')
        :return:
        """
        new_row = np.concatenate((position, velocity, speed, course))
        if self._estimation_history is None:
            # Compute rotations in radians around x, y, z axes (get RPY and convert to radians?)
            self._estimation_history = new_row.reshape(-1, 8)
        else:
            window_length = self.get_parameter('misc.variance_estimation_length').get_parameter_value().integer_value
            assert window_length > 0, f'Window length for estimating variances should be >0 ({window_length} ' \
                                      f'provided).'
            obs_count = len(self._estimation_history)
            assert 0 <= obs_count <= window_length
            if obs_count == window_length:
                # Pop oldest values
                self._estimation_history = np.delete(self._estimation_history, 0, 0)

            # Add newest values
            self._estimation_history = np.vstack((self._estimation_history, new_row))

    def _setup_map_update_timer(self) -> rclpy.timer.Timer:
        """Sets up a timer to throttle map update requests.

        Initially map updates were triggered in VehicleGlobalPosition message callbacks, but were moved to a separate
        timer since map updates may be needed even if the EKF2 filter does not publish a global position reference (e.g.
        when GPS fusion is turned off in the EKF2_AID_MASK).

        :return: The timer instance
        """
        timer_period = self.get_parameter('map_update.update_delay').get_parameter_value().integer_value
        assert_type(timer_period, int)
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
            assert_type(lat, get_args(Union[int, float]))
            assert_type(lon, get_args(Union[int, float]))
            assert_type(alt, get_args(Union[int, float]))
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
        initial_guess = self.get_parameter('map_update.initial_guess').get_parameter_value().double_array_value
        if not (len(initial_guess) == 2 and all(isinstance(x, float) for x in initial_guess)):
            lat, lon = None, None
        else:
            lat, lon = initial_guess[0], initial_guess[1]

        return lat, lon, self.get_parameter('map_update.default_altitude').get_parameter_value().double_value

    # TODO: update docstring - local position stuff was removed
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
        assert_type(latlonalt, LatLonAlt)

        # If altitude was not available in VehicleGlobalPosition, try to get it from VehicleLocalPosition
        if latlonalt.alt is None:
            self.get_logger().debug('Could not get altitude from VehicleGlobalPosition - trying VehicleLocalPosition '
                                    'instead.')
            latlonalt = LatLonAlt(latlonalt.lat, latlonalt.lon, self._alt_from_vehicle_local_position())

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

    def _import_class(self, class_name: str, module_name: str) -> object:
        """Dynamically imports class from given module if not yet imported

        :param class_name: Name of the class to import
        :param module_name: Name of module that contains the class
        :return: Imported class
        """
        if module_name not in sys.modules:
            self.get_logger().info(f'Importing module {module_name}.')
            importlib.import_module(module_name)
        imported_class = getattr(sys.modules[module_name], class_name, None)
        assert imported_class is not None, f'{class_name} was not found in module {module_name}.'
        return imported_class

    def _declare_ros_params(self) -> None:
        """Declares ROS parameters

        Uses defaults from :py:mod:`python_px4_ros2_map_nav.ros_param_defaults`. Note that some parameters are declared
        as read_only and cannot be changed at runtime.

        :return:
        """
        read_only = ParameterDescriptor(read_only=True)
        namespace = 'wms'
        self.declare_parameters(namespace, [
            ('url', Defaults.WMS_URL, read_only),
            ('version', Defaults.WMS_VERSION, read_only),
            ('layer', Defaults.WMS_LAYER),
            ('srs', Defaults.WMS_SRS),
            ('request_timeout', Defaults.WMS_REQUEST_TIMEOUT)
        ])

        namespace = 'misc'
        self.declare_parameters(namespace, [
            ('mock_gps_selection', Defaults.MISC_MOCK_GPS_SELECTION),
            ('export_position', Defaults.MISC_EXPORT_POSITION),
            ('export_projection', Defaults.MISC_EXPORT_PROJECTION),
            ('max_pitch', Defaults.MISC_MAX_PITCH),
            ('variance_estimation_length', Defaults.MISC_VARIANCE_ESTIMATION_LENGTH),
            ('min_match_altitude', Defaults.MISC_MIN_MATCH_ALTITUDE)
        ])

        namespace = 'map_update'
        self.declare_parameters(namespace, [
            ('initial_guess', None),
            ('update_delay', Defaults.MAP_UPDATE_UPDATE_DELAY, read_only),
            ('default_altitude', Defaults.MAP_UPDATE_DEFAULT_ALTITUDE),
            ('gimbal_projection', Defaults.MAP_UPDATE_GIMBAL_PROJECTION),
            ('max_map_radius', Defaults.MAP_UPDATE_MAP_RADIUS_METERS_DEFAULT),
            ('update_map_center_threshold', Defaults.MAP_UPDATE_UPDATE_MAP_CENTER_THRESHOLD),
            ('update_map_radius_threshold', Defaults.MAP_UPDATE_UPDATE_MAP_RADIUS_THRESHOLD),
            ('max_pitch', Defaults.MAP_UPDATE_MAX_PITCH)
        ])

        namespace = 'matcher'
        self.declare_parameters(namespace, [
            ('class', Defaults.MATCHER_CLASS, read_only),
            ('params_file', Defaults.MATCHER_PARAMS_FILE, read_only)
        ])

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
        assert_type(ekf2_timestamp_usec, int)
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
                assert_type(publish, bool)
                self._topics.get(self.PUBLISH_KEY).update({topic_name: self._create_publisher(topic_name, class_)})

            subscribe = topic.get(self.SUBSCRIBE_KEY, None)
            if subscribe is not None:
                assert_type(subscribe, bool)
                self._topics.get(self.SUBSCRIBE_KEY).update({topic_name: self._create_subscriber(topic_name, class_)})

        self.get_logger().info(f'Topics setup complete:\n{self._topics}.')

    def _create_publisher(self, topic_name: str, class_: object) -> rclpy.publisher.Publisher:
        """Sets up an rclpy publisher.

        :param topic_name: Name of the microRTPS topic
        :param class_: Message definition class (e.g. px4_msgs.msg.VehicleGpsPosition)
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
        assert_type(latlon, get_args(Union[LatLon, LatLonAlt]))
        assert_type(radius_meters, get_args(Union[int, float]))
        corner_distance = math.sqrt(2) * radius_meters  # Distance to corner of square enclosing circle of radius
        ul = self._move_distance(latlon, (-45, corner_distance))
        lr = self._move_distance(latlon, (135, corner_distance))
        return BBox(ul.lon, lr.lat, lr.lon, ul.lat)

    def _distance(self, latlon1: Union[LatLon, LatLonAlt], latlon2: Union[LatLon, LatLonAlt]) -> float:
        """Returns distance between two points in meters.

        The distance computation is based on latitude and longitude only and ignores altitude.

        :param latlon1: The first point
        :param latlon2: The second point
        :return: The ground distance in meters between the two points
        """
        assert_type(latlon1, get_args(Union[LatLon, LatLonAlt]))
        assert_type(latlon2, get_args(Union[LatLon, LatLonAlt]))
        _, __, dist = self._geod.inv(latlon1.lon, latlon1.lat, latlon2.lon, latlon2.lat)
        return dist

    def _move_distance(self, latlon: Union[LatLon, LatLonAlt], azmth_dist: Tuple[Union[int, float], Union[int, float]])\
            -> LatLon:
        """Returns the point that is a given distance in the direction of azimuth from the origin point.

        :param latlon: Origin point
        :param azmth_dist: Tuple containing azimuth in degrees and distance in meters: (azimuth, distance)
        :return: The point that is given meters away in the azimuth direction from origin
        """
        assert_type(azmth_dist, tuple)
        assert_type(latlon, get_args(Union[LatLon, LatLonAlt]))
        azmth, dist = azmth_dist  # TODO: silly way of providing these args just to map over a zipped list in _update_map, fix it
        assert_type(azmth, get_args(Union[int, float]))
        assert_type(dist, get_args(Union[int, float]))
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
        assert_type(dim, Dim)
        diagonal = math.ceil(math.sqrt(dim.width ** 2 + dim.height ** 2))
        assert_type(diagonal, int)  # TODO: What if this is float?
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
        assert_type(map_size, tuple)
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
        assert_type(declared_size, tuple)
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
        rpy = self._get_camera_set_rpy()
        if rpy is None:
            self.get_logger().warn('Could not get RPY - cannot project gimbal FOV.')
            return None

        # Adjust pitch for projection so # TODO: this assumes 180 deg roll?
        pitch = -(90 + rpy.pitch)
        if pitch < 0:
            # Gimbal pitch and yaw flip over when abs(gimbal_yaw) should go over 90, adjust accordingly
            pitch = 180 + pitch
        rpy = (pitch, rpy.roll, rpy.yaw)

        r = Rotation.from_euler('XYZ', list(rpy), degrees=True).as_matrix()
        e = np.hstack((r, np.expand_dims(translation, axis=1)))
        assert_shape(e, (3, 4))

        if self._camera_info is None:
            self.get_logger().warn('Could not get camera info - cannot project gimbal FOV.')
            return None

        # Intrinsic matrix
        k = np.array(self._camera_info.k).reshape([3, 3])

        # Project image corners to z=0 plane (ground)
        h = inv_homography_from_k_and_e(k, e)
        if h is None:
            self.get_logger().warn('Could not invert homography matrix - cannot project gimbal FOV.')
            return None

        img_dim = self._img_dim()
        if img_dim is None:
            self.get_logger().warn('Could determine image dimensions- cannot project gimbal FOV.')
            return None
        else:
            assert_type(h, np.ndarray)
            # noinspection PyTypeChecker
            dst_corners, _ = get_fov_and_c(img_dim, h)
            dst_corners = dst_corners.squeeze()

        return dst_corners

    def _vehicle_local_position_ref_latlonalt(self) -> Optional[LatLonAlt]:
        """Returns vehicle local frame reference origin

        :return: Local reference frame origin in WGS84, or None if not available
        """
        if self._vehicle_local_position is None:
            self.get_logger().warn('Could not get vehicle local position - returning None as local frame reference.')
            return None

        if self._vehicle_local_position.xy_global is True and self._vehicle_local_position.z_global is True:
            assert_type(self._vehicle_local_position.timestamp, int)
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
            pitch = self._camera_pitch()  # TODO: _project_gimbal_fov uses _get_camera_rpy - redundant calls  # TODO: this logic uses old pitch origin (nadir=0)
            if pitch is None:
                self.get_logger().warn('Camera pitch not available, cannot project gimbal field of view.')
                return None

            pitch_from_nadir = 90 + pitch

            #assert 0 <= abs(pitch) <= 90, f'Pitch {pitch} was outside of expected bounds [0, 90].' # TODO: need to handle outside of bounds, cannot assert
            pitch_rad = math.radians(pitch_from_nadir)
            assert origin.alt is not None
            assert hasattr(origin, 'alt')
            hypotenuse = origin.alt * math.tan(pitch_rad)  # Distance from camera origin to projected principal point
            cx = hypotenuse*math.sin(pitch_rad)
            cy = hypotenuse*math.cos(pitch_rad)
            translation = np.array([cx, cy, origin.alt])
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
        assert_type(center, get_args(Union[LatLon, LatLonAlt]))
        assert_type(radius, get_args(Union[int, float]))
        max_radius = self.get_parameter('map_update.max_map_radius').get_parameter_value().integer_value
        # TODO: need to recover from this, e.g. if its more than max_radius, warn and use max instead. Users could crash this by setting radius to above max radius
        assert 0 < radius <= max_radius, f'Radius should be between 0 and {max_radius}.'

        bbox = self._get_bbox(center, radius)  # TODO: should these things be moved to args? Move state related stuff up the call stack all in the same place. And isnt this a static function anyway?
        assert_type(bbox, BBox)

        map_size = self._map_size_with_padding()
        if map_size is None:
            self.get_logger().warn('Map size not yet available - skipping WMS request.')
            return None

        # Build and send WMS request
        layer_str = self.get_parameter('wms.layer').get_parameter_value().string_value
        srs_str = self.get_parameter('wms.srs').get_parameter_value().string_value
        assert_type(layer_str, str)
        assert_type(srs_str, str)
        try:
            self.get_logger().info(f'Getting map for bbox: {bbox}, layer: {layer_str}, srs: {srs_str}.')
            if self._wms_results is not None:
                assert self._wms_results.ready(), f'Update map was called while previous results were not yet ready.'  # Should not happen - check _should_update_map conditions
            self._wms_results = self._wms_pool.starmap_async(
                WMSClient.worker, [(center, radius, bbox, map_size, layer_str, srs_str)],
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
        assert_type(result, MapFrame)
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

    def image_raw_callback(self, msg: Image) -> None:
        """Handles latest image frame from camera.

        For every image frame, uses :meth:`~_should_match` to determine whether a new :meth:`_match` call needs to be
        made to the neural network. Inputs for the :meth:`_match` call are collected with :meth:`~_match_inputs` and
        saved into :py:attr:`~_stored_inputs` for later use. When the match call returns,
        the :meth:`~_matching_worker_callback` will use the stored inputs for post-processing the matches based on
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
        assert_type(msg, Image)

        cv_image = self._cv_bridge.imgmsg_to_cv2(msg, self.IMAGE_ENCODING)

        # Check that image dimensions match declared dimensions
        img_size = self._declared_img_size()
        if img_size is not None:
            cv_img_shape = cv_image.shape[0:2]
            assert cv_img_shape == img_size, f'Converted cv_image shape {cv_img_shape} did not match declared image ' \
                                             f'shape {img_size}.'

        # Process image frame
        # TODO: save previous image frame and check that new timestamp is greater
        image_frame = ImageFrame(cv_image, msg.header.frame_id, timestamp)

        # TODO: store image_frame as self._image_frame and move the stuff below into a dedicated self._matching_timer?
        if self._should_match():
            assert self._matching_results is None or self._matching_results.ready()
            inputs = self._match_inputs(image_frame)
            for k, v in inputs.items():
                if v is None:
                    self.get_logger().warn(f'Key {k} value {v} in match input arguments, cannot process matches.')
                    return

            camera_yaw = inputs.get('camera_yaw', None)
            map_frame = inputs.get('map_frame', None)
            img_dim = inputs.get('img_dim', None)
            assert all((camera_yaw, map_frame, img_dim))  # Redundant (see above 'for k, v in inputs.items(): ...')

            self._stored_inputs = inputs
            map_cropped = inputs.get('map_cropped')
            assert_type(map_cropped, np.ndarray)

            self.get_logger().debug(f'Matching image with timestamp {image_frame.timestamp} to map.')
            self._match(image_frame, map_cropped)

    def _camera_yaw(self) -> Optional[int]:  # TODO: int or float?
        """Returns camera yaw in degrees.

        :return: Camera yaw in degrees, or None if not available
        """
        rpy = self._get_camera_set_rpy()
        if rpy is None:
            self.get_logger().warn(f'Could not get camera RPY - cannot return yaw.')
            return None
        assert_type(rpy, RPY)
        camera_yaw = rpy.yaw
        return camera_yaw

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
        if self._gimbal_device_set_attitude is None:
            self.get_logger().warn('No VehicleAttitude message has been received yet.')
            return None
        else:
            gimbal_set_attitude = Rotation.from_quat(self._gimbal_device_set_attitude.q)
            return gimbal_set_attitude

    def _get_camera_set_rpy(self) -> Optional[RPY]:
        """Returns roll-pitch-yaw tuple in NED frame of camera attitude setting.

        True camera attitude may be different if gimbal has not yet stabilized.

        :return: An :class:`util.RPY` tuple
        """
        gimbal_set_attitude = self._get_gimbal_set_attitude()
        if gimbal_set_attitude is None:
            self.get_logger().warn('Gimbal attitude not available, cannot return RPY.')
            return None
        assert_type(gimbal_set_attitude, Rotation)
        gimbal_euler = gimbal_set_attitude.as_euler('xyz', degrees=True)

        if self._vehicle_local_position is None:
            self.get_logger().warn('VehicleLocalPosition is unknown, cannot get heading. Cannot return RPY.')
            return None

        heading = self._vehicle_local_position.heading
        heading = math.degrees(heading)
        assert -180 <= heading <= 180, f'Unexpected heading value: {heading} degrees ([-180, 180] expected).'

        self.get_logger().debug('Assuming stabilized gimbal - ignoring vehicle intrinsic pitch and roll for camera RPY.')
        self.get_logger().debug('Assuming zero roll for camera RPY.')  # TODO remove zero roll assumption

        gimbal_yaw = gimbal_euler[0] - 180
        yaw = heading - gimbal_yaw
        yaw = yaw % 360
        if abs(yaw) > 180:  # Important: >, not >= (because we are using mod 180 operation below)
            yaw = yaw % 180 if yaw < 0 else yaw % -180  # Make the compound yaw between -180 and 180 degrees
        roll = 0  # TODO remove zero roll assumption
        pitch = -gimbal_euler[1]
        rpy = RPY(roll, pitch, yaw)

        return rpy

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
        assert_type(msg.timestamp, int)
        self._vehicle_local_position = msg
        self._sync_timestamps(self._vehicle_local_position.timestamp)

    def _get_dynamic_map_radius(self, altitude: Union[int, float]) -> int:
        """Returns map radius that adjusts for camera altitude.

        :param altitude: Altitude of camera in meters
        :return: Suitable map radius in meters
        """
        assert_type(altitude, get_args(Union[int, float]))
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
        assert_type(radius, get_args(Union[int, float]))
        assert_type(center, get_args(Union[LatLon, LatLonAlt]))
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
        assert_type(radius, get_args(Union[int, float]))
        assert_type(center, get_args(Union[LatLon, LatLonAlt]))

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

    def _publish_mock_gps_msg(self, latlonalt: np.ndarray, velocities: np.ndarray, speed: float, course: float,
                              sd: np.ndarray, var: np.ndarray,  selection: int) -> None:
        """Publishes a mock :class:`px4_msgs.msg.VehicleGpsPosition` out of estimated position, velocities and errors.

        :param latlonalt: Estimated vehicle position
        :param velocities: Estimated vehicle velocities (vx, vy, vz) in meters per second
        :param speed: Speed (norm of velocity vector) of vehicle in meters per second
        :param course: Direction of movement of vehicle (not heading) in radians in NED frame
        :param sd: Estimated x, y, z position and velocity standard deviations
        :param var: Estimated x, y, z position and velocity variances
        :param selection: GPS selection (see :class:`px4_msgs.msg.VehicleGpsPosition` for comment)
        :return:
        """
        # TODO: check inputs?
        msg = VehicleGpsPosition()
        msg.timestamp = self._get_ekf2_time()
        msg.fix_type = 3
        msg.s_variance_m_s = var[6]
        msg.c_variance_rad = var[7]
        msg.lat = int(latlonalt[0] * 1e7)
        msg.lon = int(latlonalt[1] * 1e7)
        msg.alt = int(latlonalt[2] * 1e3)
        msg.alt_ellipsoid = msg.alt
        msg.eph = max(sd[0:2])
        msg.epv = sd[2]
        msg.hdop = 5.
        msg.vdop = 5.
        msg.vel_m_s = speed
        msg.vel_n_m_s = velocities[0]
        msg.vel_e_m_s = velocities[1]
        msg.vel_d_m_s = velocities[2]
        msg.cog_rad = course
        msg.vel_ned_valid = True
        msg.satellites_used = np.iinfo(np.uint8).max
        msg.time_utc_usec = int(time.time() * 1e6)
        msg.heading = np.nan
        msg.heading_offset = np.nan
        msg.selected = selection

        #self._topics.get(self.PUBLISH_KEY).get(self.VEHICLE_GPS_POSITION_TOPIC_NAME)\
        #    .publish(msg)

    # TODO: need to return real! cmaera pitch, not set pitch
    def _camera_pitch(self) -> Optional[Union[int, float]]:
        """Returns camera pitch in degrees relative to nadir.

        Pitch of 0 degrees is a nadir facing camera, while a positive pitch of 90 degrees means the camera is facing
        the direction the vehicle is heading (facing horizon).

        :return: Camera pitch in degrees, or None if not available
        """
        rpy = self._get_camera_set_rpy()
        if rpy is None:
            self.get_logger().warn('Gimbal RPY not available, cannot compute camera pitch.')
            return None
        assert_type(rpy, RPY)
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

    def _match_inputs(self, image_frame: ImageFrame) -> dict:
        """Returns a dictionary snapshot of the input data required to perform and process a match.

        Processing of matches is asynchronous, so this method provides a way of taking a snapshot of the input arguments
        to :meth:`_process_matches` from the time image used for the matching was taken.

        The dictionary has the following data:
            map_frame - np.ndarray map_frame to match
            k - np.ndarray Camera intrinsics matrix of shape (3x3) from CameraInfo
            camera_yaw - float Camera yaw in radians
            vehicle_attitude - Rotation Vehicle attitude
            map_dim_with_padding - Dim map dimensions including padding for rotation
            img_dim - Dim image dimensions
            map_cropped - np.ndarray Rotated and cropped map raster from map_frame.image

        :param image_frame: The image frame from the drone video
        :return: Dictionary with matching input data (give as **kwargs to _process_matches)
        """
        camera_yaw_deg = self._camera_yaw()
        camera_yaw = math.radians(camera_yaw_deg) if camera_yaw_deg is not None else None
        img_dim = self._img_dim()
        data = {
            'image_frame': image_frame,
            'map_frame': self._map_frame,
            'k': self._camera_info.k.reshape((3, 3)) if self._camera_info is not None else None,
            'camera_yaw': camera_yaw,
            'vehicle_attitude': self._get_vehicle_attitude(),
            'map_dim_with_padding': self._map_dim_with_padding(),
            'img_dim': img_dim
        }

        # Get cropped and rotated map
        if all((camera_yaw, self._map_frame, img_dim)):
            assert hasattr(self._map_frame, 'image'), 'Map frame unexpectedly did not contain the image data.'
            assert -np.pi <= camera_yaw <= np.pi, f'Unexpected gimbal yaw value: {camera_yaw} ([-pi, pi] expected).'
            data['map_cropped'] = rotate_and_crop_map(self._map_frame.image, camera_yaw, img_dim)
        else:
            data['map_cropped'] = None

        return data

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
        assert_type(max_pitch, get_args(Union[int, float]))
        camera_pitch = self._camera_pitch()
        if camera_pitch is not None:
            #if abs(camera_pitch) > max_pitch:
            if camera_pitch + 90 > max_pitch:
                self.get_logger().debug(f'Camera pitch {camera_pitch} is above limit {max_pitch}.')
                return True
            #if camera_pitch < 0:
            #    self.get_logger().warn(f'Camera pitch {camera_pitch} is negative.')
        else:
            self.get_logger().warn(f'Could not determine camera pitch.')
            return True

        return False

    def _should_match(self) -> bool:
        """Determines whether _match should be called based on whether previous match is still being processed.

        Match should be attempted if (1) there are no pending match results, (2) camera pitch is not too high (e.g.
        facing horizon instead of nadir), and (3) drone is not flying too low.

        :return: True if matching should be attempted
        """
        # Check condition (1) - that a request is not already running
        if not (self._matching_results is None or self._matching_results.ready()):  # TODO: handle timeouts, failures for matching results
            return False

        # Check condition (2) - whether camera pitch is too large
        max_pitch = self.get_parameter('misc.max_pitch').get_parameter_value().integer_value
        if self._camera_pitch_too_high(max_pitch):
            self.get_logger().warn(f'Camera pitch is not available or above limit {max_pitch}. Skipping matching.')
            return False

        # Check condition (3) - whether vehicle altitude is too low
        min_alt = self.get_parameter('misc.min_match_altitude').get_parameter_value().integer_value
        altitude = self._alt_from_vehicle_local_position()
        if not isinstance(min_alt, int) or altitude < min_alt:
            self.get_logger().warn(f'Altitude {altitude} was lower than minimum threshold for matching ({min_alt}) or '
                                   f'could not be determined. Skipping matching.')
            return False

        return True

    def matching_worker_error_callback(self, e: BaseException) -> None:
        """Error callback for matching worker.

        :return:
        """
        self.get_logger().error(f'Matching process returned and error:\n{e}\n{traceback.print_exc()}')

    def matching_worker_callback(self, results) -> None:
        """Callback for matching worker.

        Retrieves latest :py:attr:`~_stored_inputs` and uses them to call :meth:`~_process_matches`. The stored inputs
        are needed so that the post-processing is done using the same state information that was used for initiating
        the match in the first place. For example, camera pitch may have changed since then (e.g. if match takes 100ms)
        and current camera pitch should therefore not be used for processing the matches.

        :return:
        """
        mkp_img, mkp_map = results[0]
        assert_len(mkp_img, len(mkp_map))
        self._process_matches(mkp_img, mkp_map, **self._stored_inputs)

    def _compute_xyz_distances(self, position: LatLonAlt, origin: LatLonAlt) -> Optional[Tuple[float, float, float]]:
        """Computes distance in meters in x, y and z (NED frame) dimensions from origin to position

        :param origin: WGS84 coordinates of origin
        :param position: WGS84 coordinates of position
        :return: Tuple containing x, y and z coordinates (meters)
        """
        assert_type(position, LatLonAlt)
        assert_type(origin, LatLonAlt)

        lats_orig = (origin.lat, origin.lat)
        lons_orig = (origin.lon, origin.lon)
        lats_term = (origin.lat, position.lat)
        lons_term = (position.lon, origin.lon)
        _, __, dist = self._geod.inv(lons_orig, lats_orig, lons_term, lats_term)

        lat_diff = math.copysign(dist[1], position.lat - origin.lat)
        lon_diff = math.copysign(dist[0], position.lon - origin.lon)

        alt = position.alt - origin.alt

        return lat_diff, lon_diff, alt

    def _store_extrinsic_guess(self, r: np.ndarray, t: np.ndarray) -> None:
        """Stores rotation and translation vectors for use by :func:`cv2.solvePnPRansac` in :meth:`~_process_matches`.

        Assumes previous solution to the PnP problem will be close to the new solution. See also
        :meth:`~_retrieve_extrinsic_guess`.

        :param r: Rotation vector
        :param t: Translation vector
        :return:
        """
        self._r = r
        self._t = t

    def _retrieve_extrinsic_guess(self) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """Retrieves stored rotation and translation vectors for use by :func:`cv2.solvePnPRansac` in
         :meth:`~_process_matches`.

        Assumes previous solution to the PnP problem will be close to the new solution. See also
        :meth:`~_store_extrinsic_guess`.

        # TODO: require that timestamp of previous solution is not too old

        :return: Tuple with stored rotation and translation vectors, or tuple of Nones if not available
        """
        return self._r, self._t

    def _estimate_velocities(self, current_position: ImageFrame, previous_position: ImageFrame) -> np.ndarray:
        """Estimates x, y and z velocities in m/s from current and previous position.

        :param current_position: Current estimated position
        :param previous_position: Previous estimated position
        :return: Tuple of x, y and z velocity in m/s
        """
        time_diff_sec = (current_position.timestamp - previous_position.timestamp) / 1e6
        diff_position = np.array(self._compute_xyz_distances(current_position.position,
                                                             previous_position.position))
        velocities = diff_position / time_diff_sec
        return velocities

    def _process_matches(self, mkp_img: np.ndarray, mkp_map: np.ndarray, image_frame: ImageFrame, map_frame: MapFrame,
                         k: np.ndarray, camera_yaw: float, vehicle_attitude: Rotation, map_dim_with_padding: Dim,
                         img_dim: Dim, map_cropped: Optional[np.ndarray] = None)\
            -> None:
        """Process the matching image and map keypoints into an outgoing :class:`px4_msgs.msg.VehicleGpsPosition`
        message.

        The API for this method is designed so that the dictionary returned by :meth:`~_match_inputs` can be passed
        onto this method as keyword arguments (**kwargs).

        :param mkp_img: Matching keypoints in drone image
        :param mkp_map: Matching keypoints in map raster
        :param image_frame: The drone image
        :param map_frame: The map raster
        :param k: Camera intrinsics matrix from CameraInfo from time of match (from _match_inputs)
        :param camera_yaw: Camera yaw in radians from time of match (from _match_inputs)  # Maybe rename map rotation so less confusion with gimbal attitude stuff extractd from rotation matrix?
        :param vehicle_attitude: Vehicle attitude
        :param map_dim_with_padding: Map dimensions with padding from time of match (from _match_inputs)
        :param img_dim: Drone image dimensions from time of match (from _match_inputs)
        :param map_cropped: Optional map cropped image
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
        mkp_map_3d = np.hstack((mkp_map, padding))  # Set all world z-coordinates to zero
        dist_coeffs = np.zeros((4, 1))
        r, t = self._retrieve_extrinsic_guess()
        use_guess = True if r is not None and t is not None else False
        _, r, t, __ = cv2.solvePnPRansac(mkp_map_3d, mkp_img, k, dist_coeffs, r, t, useExtrinsicGuess=use_guess,
                                         iterationsCount=10)
        self._store_extrinsic_guess(r, t)
        r, _ = cv2.Rodrigues(r)
        e = np.hstack((r, t))  # Extrinsic matrix (for homography estimation)
        pos = -r.T @ t  # Inverse extrinsic (for computing camera position in object coordinates)
        h = inv_homography_from_k_and_e(k, e)
        if h is None:
            self.get_logger().warn('Could not invert homography matrix, cannot estimate position.')
            return None

        # Field of view in both pixel (rotated and cropped map raster) and WGS84 coordinates
        h_wgs84 = pix_to_wgs84_ @ h
        fov_pix, c_pix = get_fov_and_c(img_dim, h)
        fov_wgs84, c_wgs84 = get_fov_and_c(img_dim, h_wgs84)
        image_frame.fov = fov_wgs84

        # Compute altitude scaling:
        # Altitude in t is in rotated and cropped map raster pixel coordinates. We can use fov_pix and fov_wgs84 to
        # find out the right scale in meters. Distance in pixels is computed from lower left and lower right corners
        # of the field of view (bottom of fov assumed more stable than top), while distance in meters is computed from
        # the corresponding WGS84 latitude and latitude coordinates.
        distance_in_pixels = np.linalg.norm(fov_pix[1]-fov_pix[2])  # fov_pix[1]: lower left, fov_pix[2]: lower right
        distance_in_meters = self._distance(LatLon(*fov_wgs84[1].squeeze().tolist()),
                                            LatLon(*fov_wgs84[2].squeeze().tolist()))
        altitude_scaling = abs(distance_in_meters / distance_in_pixels)

        # Translation in WGS84
        t_wgs84 = pix_to_wgs84_ @ np.append(pos[0:2], 1)
        t_wgs84[2] = -altitude_scaling * pos[2]  # In NED frame z-coordinate is negative above ground but make altitude positive

        position = t_wgs84.squeeze().tolist()
        image_frame.position = LatLonAlt(*position)  # TODO: shcleould just ditch LatLonAlt and keep numpy arrays?

        # Check that we have everything we need to publish vehicle_gps_position
        if not all(image_frame.position) or any(map(np.isnan, image_frame.position)):
            self.get_logger().debug('Could not determine global position. Cannot create mock GPS position message.')
            return None

        # Convert estimated rotation to attitude quaternion for publishing
        gimbal_estimated_attitude = Rotation.from_matrix(r.T)  # in rotated map pixel frame
        gimbal_estimated_attitude *= Rotation.from_rotvec(-(np.pi/2) * np.array([1, 0, 0]))  # camera body pose
        gimbal_estimated_attitude *= Rotation.from_rotvec(camera_yaw * np.array([0, 0, 1]))  # unrotated map pixel frame

        # Re-arrange axes from unrotated (original) map pixel frame to NED frame
        rotvec = gimbal_estimated_attitude.as_rotvec()
        gimbal_estimated_attitude = Rotation.from_rotvec([-rotvec[1], rotvec[0], rotvec[2]])

        # TODO: figure out a way to get vehicle attitude from gimbal attitude
        vehicle_attitude_estimate = vehicle_attitude
        vehicle_attitude_estimate_rpy = vehicle_attitude_estimate.as_euler('XYZ')
        quaternion = vehicle_attitude_estimate.as_quat()

        # noinspection PyUnreachableCode
        if __debug__:
            # Visualization of matched keypoints and field of view boundary
            number_str_len = 7
            accuracy = 2
            gimbal_rpy_deg = RPY(*gimbal_estimated_attitude.as_euler('XYZ', degrees=True))
            gimbal_rpy_text = f'Gimbal roll: {str(round(gimbal_rpy_deg.roll, accuracy)).rjust(number_str_len)}, ' \
                              f'pitch: {str(round(gimbal_rpy_deg.pitch, accuracy)).rjust(number_str_len)}, ' \
                              f'yaw: {str(round(gimbal_rpy_deg.yaw, accuracy)).rjust(number_str_len)}.'

            visualize_homography('Keypoint matches and FOV', gimbal_rpy_text, image_frame.image,
                                 map_cropped, mkp_img, mkp_map, fov_pix)

        mock_gps_selection = self.get_parameter('misc.mock_gps_selection').get_parameter_value().integer_value
        if self._previous_image_frame is not None:
            velocities = self._estimate_velocities(image_frame, self._previous_image_frame)
            speed = np.linalg.norm(velocities)
            course = np.arctan2(velocities[1], velocities[0])
            self._push_estimates(position, velocities, np.array([speed]), np.array([course]))
            if self._variance_window_full():
                sd = np.std(self._estimation_history, axis=0)
                var = np.var(self._estimation_history, axis=0)
                self._publish_mock_gps_msg(position, velocities, speed, course, sd, var, mock_gps_selection)
            else:
                self.get_logger().debug('Waiting to get more data to estimate position error - not publishing mock GPS '
                                        'message yet...')

        export_geojson = self.get_parameter('misc.export_position').get_parameter_value().string_value
        if export_geojson is not None:
            self._export_position(image_frame.position, image_frame.fov, export_geojson)

        self._previous_image_frame = image_frame

    def _export_position(self, position: Union[LatLon, LatLonAlt], fov: np.ndarray, filename: str) -> None:
        """Exports the computed position and field of view (FOV) into a geojson file.

        The GeoJSON file is not used by the node but can be accessed by GIS software to visualize the data it contains.

        :param position: Computed camera position
        :param: fov: Field of view of camera
        :param filename: Name of file to write into
        :return:
        """
        assert_type(position, get_args(Union[LatLon, LatLonAlt]))
        assert_type(fov, np.ndarray)
        assert_type(filename, str)
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
        assert self._matching_results is None or self._matching_results.ready()
        self._matching_results = self._matching_pool.starmap_async(
            self._kp_matcher.worker,
            [(image_frame.image, map_cropped)],
            callback=self.matching_worker_callback,
            error_callback=self.matching_worker_error_callback
        )

    def terminate_wms_pool(self):
        """Terminates the WMS Pool.

        :return:
        """
        if self._wms_pool is not None:
            self.get_logger().info('Terminating WMS pool.')
            self._wms_pool.terminate()

    def destroy_timers(self):
        """Destroys the map update timer.

        :return:
        """
        if self._map_update_timer is not None:
            self.get_logger().info('Destroying map update timer.')
            assert_type(self._map_update_timer, rclpy.timer.Timer)
            self._map_update_timer.destroy()
