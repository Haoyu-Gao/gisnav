"""Module that contains the BaseNode ROS 2 node."""
import sys
import rclpy
import traceback
import math
import numpy as np
import cv2
import importlib
import os
import yaml
import torch


from abc import ABC, abstractmethod
from multiprocessing.pool import Pool
#from multiprocessing.pool import ThreadPool as Pool  # Rename 'Pool' to keep same interface
from typing import Optional, Union, Tuple, get_args, Callable
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from rcl_interfaces.msg import ParameterDescriptor
from cv_bridge import CvBridge
from scipy.spatial.transform import Rotation
from sensor_msgs.msg import Image
from geographic_msgs.msg import BoundingBox, GeoPoint as ROSGeoPoint
from shapely.geometry import box

from gisnav.data import Dim, ImageData, MapData, Attitude, DataValueError, InputData, ImagePair, AsyncPoseQuery, \
    AsyncWMSQuery, ContextualMapData, FixedCamera, Img, Pose, Position, Altitude, BBox
from gisnav.geo import GeoPoint, GeoSquare, GeoTrapezoid
from gisnav.assertions import assert_type, assert_len, assert_ndim, assert_shape
from gisnav.pose_estimators.pose_estimator import PoseEstimator
from gisnav.wms import WMSClient
from gisnav.autopilots.autopilot import Autopilot

from gisnav_msgs.msg import OrthoImage3D
from gisnav_msgs.srv import GetMap

class BaseNode(Node, ABC):
    """ROS 2 node that publishes position estimate based on visual match of drone video to map of same location"""

    # Process counts for multiprocessing pools
    _MATCHER_PROCESS_COUNT = 1  # should be 1

    # Encoding of input video (input to CvBridge)
    # e.g. gscam2 only supports bgr8 so this is used to override encoding in image header
    _IMAGE_ENCODING = 'bgr8'

    # Altitude in meters used for DEM request to get local origin elevation
    _DEM_REQUEST_ALTITUDE = 100

    # region ROS Parameter Defaults
    ROS_D_MISC_AUTOPILOT = 'gisnav.autopilots.px4_micrortps.PX4microRTPS'
    """Default autopilot adapter"""

    ROS_D_STATIC_CAMERA = False
    """Default value for static camera flag (true for static camera facing down from vehicle body)"""

    ROS_D_MISC_ATTITUDE_DEVIATION_THRESHOLD = 10
    """Magnitude of allowed attitude deviation of estimate from expectation in degrees"""

    ROS_D_MISC_MAX_PITCH = 30
    """Default maximum camera pitch from nadir in degrees for attempting to estimate pose against reference map

    .. seealso::
        :py:attr:`.ROS_D_MAP_UPDATE_MAX_PITCH` 
        :py:attr:`.ROS_D_MAP_UPDATE_GIMBAL_PROJECTION`
    """

    ROS_D_MISC_MIN_MATCH_ALTITUDE = 80
    """Default minimum ground altitude in meters under which matches against map will not be attempted"""

    ROS_D_MAP_UPDATE_UPDATE_DELAY = 1
    """Default delay in seconds for throttling WMS GetMap requests

    When the camera is mounted on a gimbal and is not static, this delay should be set quite low to ensure that whenever
    camera field of view is moved to some other location, the map update request will follow very soon after. The field
    of view of the camera projected on ground generally moves *much faster* than the vehicle itself.
    
    .. note::
        This parameter provides a hard upper limit for WMS GetMap request frequency. Even if this parameter is set low, 
        WMS GetMap requests will likely be much less frequent because they will throttled by the conditions set in  
        :meth:`._should_update_map` (private method - see source code for reference).
    """

    ROS_D_MAP_UPDATE_GIMBAL_PROJECTION = True
    """Default flag to enable map updates based on expected center of field of view (FOV) projected onto ground

    When this flag is enabled, map rasters are retrieved for the expected center of the camera FOV instead of the
    expected position of the vehicle, which increases the chances that the FOV is fully contained in the map raster.
    This again increases the chances of getting a good pose estimate.
    
    .. seealso::
        :py:attr:`.ROS_D_MISC_MAX_PITCH`
        :py:attr:`.ROS_D_MAP_UPDATE_MAX_PITCH`
    """

    ROS_D_MAP_UPDATE_MAX_PITCH = 30
    """Default maximum camera pitch from nadir in degrees for attempting to update the stored map

    This limit only applies when camera field of view (FOV) projection is enabled. This value will prevent unnecessary 
    WMS GetMap requests when the camera is looking far into the horizon and it would be unrealistic to get a good pose 
    estimate against a map.

    .. seealso::
        :py:attr:`.ROS_D_MISC_MAX_PITCH`
        :py:attr:`.ROS_D_MAP_UPDATE_GIMBAL_PROJECTION`
    """

    ROS_D_MAP_UPDATE_MAX_MAP_RADIUS = 400
    """Default maximum map radius (half of map width) in meters for WMS GetMap request bounding boxes

    This limit prevents unintentionally requesting very large maps if camera field of view is projected far into the
    horizon. This may happen e.g. if :py:attr:`.ROS_D_MAP_UPDATE_MAX_PITCH` is set too high relative to the camera's
    vertical view angle. If the WMS server back-end needs to e.g. piece the large map together from multiple files the 
    request might timeout in any case.
    """

    ROS_D_MAP_UPDATE_UPDATE_MAP_AREA_THRESHOLD = 0.85
    """Default map bounding box area intersection threshold that prevents a new map from being retrieved

    This prevents unnecessary WMS GetMap requests to replace an old map with a new map that covers almost the same area.
    """

    ROS_D_POSE_ESTIMATOR_CLASS = 'gisnav.pose_estimators.loftr.LoFTRPoseEstimator'
    """Default :class:`.PoseEstimator` to use for estimating pose camera pose against reference map"""

    ROS_D_POSE_ESTIMATOR_PARAMS_FILE = 'config/loftr_params.yaml'
    """Default parameter file with args for the default :class:`.PoseEstimator`'s :meth:`.PoseEstimator.initializer`"""

    ROS_D_DEBUG_EXPORT_POSITION = '' # 'position.json'
    """Default filename for exporting GeoJSON containing estimated field of view and position

    Set to '' to disable
    """

    ROS_D_DEBUG_EXPORT_PROJECTION = '' # 'projection.json'
    """Default filename for exporting GeoJSON containing projected field of view (FOV) and FOV center
        
    Set to '' to disable
    """

    read_only = ParameterDescriptor(read_only=True)
    _ROS_PARAMS = [
        ('misc.autopilot', ROS_D_MISC_AUTOPILOT, read_only),
        ('misc.static_camera', ROS_D_STATIC_CAMERA, read_only),
        ('misc.attitude_deviation_threshold', ROS_D_MISC_ATTITUDE_DEVIATION_THRESHOLD),
        ('misc.max_pitch', ROS_D_MISC_MAX_PITCH),
        ('misc.min_match_altitude', ROS_D_MISC_MIN_MATCH_ALTITUDE),
        ('map_update.update_delay', ROS_D_MAP_UPDATE_UPDATE_DELAY, read_only),
        ('map_update.gimbal_projection', ROS_D_MAP_UPDATE_GIMBAL_PROJECTION),
        ('map_update.max_map_radius', ROS_D_MAP_UPDATE_MAX_MAP_RADIUS),
        ('map_update.update_map_area_threshold', ROS_D_MAP_UPDATE_UPDATE_MAP_AREA_THRESHOLD),
        ('map_update.max_pitch', ROS_D_MAP_UPDATE_MAX_PITCH),
        ('pose_estimator.class', ROS_D_POSE_ESTIMATOR_CLASS, read_only),
        ('pose_estimator.params_file', ROS_D_POSE_ESTIMATOR_PARAMS_FILE, read_only),
        ('debug.export_position', ROS_D_DEBUG_EXPORT_POSITION),
        ('debug.export_projection', ROS_D_DEBUG_EXPORT_PROJECTION),
    ]
    """ROS parameter configuration to declare
    
    .. note::
        Some parameters are declared read_only and cannot be changed at runtime because there is currently no way to 
        reinitialize the WMS client, pose estimator, Kalman filter, WMS map update timer, nor the autopilot or ROS 
        subscriptions.
    """
    # endregion

    def __init__(self, name: str, package_share_dir: str) -> None:
        """Initializes the ROS 2 node.

        :param name: Name of the node
        :param package_share_dir: Package share directory file path
        """
        assert_type(name, str)
        assert_type(package_share_dir, str)
        super().__init__(name, allow_undeclared_parameters=True, automatically_declare_parameters_from_overrides=True)
        self.__declare_ros_params()

        # TODO: temporary publisher, remove
        self._bbox_pub = self.create_publisher(BoundingBox, 'bbox', QoSPresetProfiles.SENSOR_DATA.value)
        self._map_sub = self.create_subscription(OrthoImage3D, 'orthoimage_3d', self._orthoimage3d_callback,
                                                 QoSPresetProfiles.SENSOR_DATA.value)
        self._map_cli = self.create_client(GetMap, 'orthoimage_3d_service')
        while not self._map_cli.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Waiting for GetMap service...')

        self._package_share_dir = package_share_dir

        self._map_update_timer = self._setup_map_update_timer()

        # Setup pose estimation pool
        pose_estimator_params_file = self.get_parameter('pose_estimator.params_file').get_parameter_value().string_value
        self._pose_estimator, self._pose_estimator_pool = self._setup_pose_estimation_pool(pose_estimator_params_file)
        self._pose_estimation_query = None  # Must check for None when using this

        # Autopilot bridge
        ap = self.get_parameter('misc.autopilot').get_parameter_value().string_value or self.ROS_D_MISC_AUTOPILOT
        ap: Autopilot = self._load_autopilot(ap)
        # TODO: implement passing init args, kwargs
        self._bridge = ap(self, self._image_raw_callback)

        # Converts image_raw to cv2 compatible image
        self._cv_bridge = CvBridge()

        # Initialize remaining properties (does not include computed properties)
        self._map_input_data = None
        self._map_input_data_prev = None
        self._fixed_camera_prev = None
        # self._image_data = None  # Not currently used / needed
        self._map_data = None
        self._pose_guess = None
        self._origin_dem_altitude = None
        self._msg = None  # orthoimage3d message from map node
        self._dem = None  # dem map data
        self._dem_req_future = None  # Store async dem service call
        self._dem_request_map_candidate = None # todo handle together with dem req future

    # region Properties
    @property
    def _package_share_dir(self) -> str:
        """ROS 2 package share directory"""
        return self.__package_share_dir

    @_package_share_dir.setter
    def _package_share_dir(self, value: str) -> None:
        assert_type(value, str)
        self.__package_share_dir = value

    @property
    def _pose_guess(self) -> Optional[Pose]:
        """Stores rotation and translation vectors for use by :func:`cv2.solvePnPRansac`.

        Assumes previous solution to the PnP problem will be close to the new solution.
        """
        return self.__pose_guess

    @_pose_guess.setter
    def _pose_guess(self, value: Optional[Pose]) -> None:
        assert_type(value, get_args(Optional[Pose]))
        self.__pose_guess = value

    @property
    def _pose_estimator(self) -> PoseEstimator:
        """Dynamically loaded :class:`.PoseEstimator`"""
        return self.__pose_estimator

    @_pose_estimator.setter
    def _pose_estimator(self, value: Optional[PoseEstimator]) -> None:
        assert issubclass(value, get_args(Optional[PoseEstimator]))
        self.__pose_estimator = value

    @property
    def _map_update_timer(self) -> rclpy.timer.Timer:
        """Timer for throttling map update WMS requests."""
        return self.__map_update_timer

    @_map_update_timer.setter
    def _map_update_timer(self, value: rclpy.timer.Timer) -> None:
        assert_type(value, rclpy.timer.Timer)
        self.__map_update_timer = value

    @property
    def _pose_estimator_pool(self) -> Pool:
        """Pool for running a :class:`.PoseEstimator` in dedicated thread (or process)"""
        return self.__pose_estimator_pool

    @_pose_estimator_pool.setter
    def _pose_estimator_pool(self, value: Pool) -> None:
        assert_type(value, Pool)
        self.__pose_estimator_pool = value

    @ property
    def _map_data(self) -> Optional[MapData]:
        """The map raster from the WMS server response along with supporting metadata."""
        return self.__map_data

    @_map_data.setter
    def _map_data(self, value: Optional[MapData]) -> None:
        assert_type(value, get_args(Optional[MapData]))
        self.__map_data = value

    @property
    def _map_input_data(self) -> InputData:
        """Inputs stored at time of launching a new asynchronous match that are needed for processing its results."""
        return self.__map_input_data

    @_map_input_data.setter
    def _map_input_data(self, value: Optional[InputData]) -> None:
        assert_type(value, get_args(Optional[InputData]))
        self.__map_input_data = value

    @property
    def _pose_estimation_query(self) -> Optional[AsyncPoseQuery]:
        """Asynchronous results and input from a pose estimation thread or process."""
        return self.__pose_estimation_query

    @_pose_estimation_query.setter
    def _pose_estimation_query(self, value: Optional[AsyncPoseQuery]) -> None:
        assert_type(value, get_args(Optional[AsyncPoseQuery]))
        self.__pose_estimation_query = value

    @property
    def _cv_bridge(self) -> CvBridge:
        """CvBridge that decodes incoming PX4-ROS 2 bridge images to cv2 images."""
        return self.__cv_bridge

    @_cv_bridge.setter
    def _cv_bridge(self, value: CvBridge) -> None:
        assert_type(value, CvBridge)
        self.__cv_bridge = value

    @property
    def _map_input_data_prev(self) -> Optional[InputData]:
        """Previous map input data"""
        return self.__map_input_data_prev

    @_map_input_data_prev.setter
    def _map_input_data_prev(self, value: Optional[InputData]) -> None:
        assert_type(value, get_args(Optional[InputData]))
        self.__map_input_data_prev = value

    @property
    def _fixed_camera_prev(self) -> Optional[FixedCamera]:
        """Previous estimated :class:`.FixedCamera`"""
        return self.__fixed_camera_prev

    @_fixed_camera_prev.setter
    def _fixed_camera_prev(self, value: Optional[FixedCamera]) -> None:
        assert_type(value, get_args(Optional[FixedCamera]))
        self.__fixed_camera_prev = value

    @property
    def _autopilot(self) -> Autopilot:
        """Autopilot bridge adapter"""
        return self.__autopilot

    @_autopilot.setter
    def _autopilot(self, value: Autopilot) -> None:
        assert_type(value, Autopilot)
        self.__autopilot = value

    @property
    def _origin_dem_altitude(self) -> Optional[float]:
        """Elevation layer (DEM) altitude at local frame origin"""
        return self.__origin_dem_altitude

    @_origin_dem_altitude.setter
    def _origin_dem_altitude(self, value: Optional[float]) -> None:
        assert_type(value, get_args(Optional[float]))
        self.__origin_dem_altitude = value
    # endregion

    # region Computed Properties
    @property
    def _altitude_scaling(self) -> Optional[float]:
        """Returns camera focal length divided by camera altitude in meters."""
        alt_agl = self._bridge.altitude_agl(self._terrain_altitude_amsl_at_position(self._bridge.global_position))
        if self._bridge.camera_data is not None and alt_agl is not None:
            return self._bridge.camera_data.fx / alt_agl
        else:
            self.get_logger().warn('Could not estimate elevation scale because camera focal length and/or vehicle '
                                   'altitude is unknown.')
            return None

    @property
    def _r_guess(self) -> Optional[np.ndarray]:
        """Gimbal rotation matrix guess (based on :class:`px4_msgs.GimbalDeviceSetAttitude` message)

        .. note::
            Should be roughly same as rotation matrix stored in :py:attr:`._pose_guess`, even though it is derived via
            a different route. If gimbal is not stabilized to its set position, the rotation matrix will be different.
        """
        static_camera = self.get_parameter('misc.static_camera').get_parameter_value().bool_value
        if self._bridge.gimbal_set_attitude is None:
            if not static_camera:
                self.get_logger().warn('Gimbal set attitude not available, will not provide pose guess.')
                return None
            else:
                if self._bridge.attitude is not None:
                    attitude = self._bridge.attitude.as_rotation()
                    attitude *= Rotation.from_euler('XYZ', [0, -np.pi/2, 0])
                    return Attitude(attitude.as_quat()).to_esd().r
                else:
                    self.get_logger().warn('Vehicle attitude not available, will not provide pose guess for static '
                                           'camera.')
                    return None
        else:
            assert_type(self._bridge.gimbal_set_attitude, Attitude)
            gimbal_set_attitude = self._bridge.gimbal_set_attitude.to_esd()  # Need coordinates in image frame, not NED
            return gimbal_set_attitude.r

    @property
    def _is_gimbal_projection_enabled(self) -> bool:
        """True if map rasters should be retrieved for projected field of view instead of vehicle position

        If this is set to false, map rasters are retrieved for the vehicle's global position instead. This is typically
        fine as long as the camera is not aimed too far in to the horizon and has a relatively wide field of view. For
        best results, this should be on to ensure the field of view is fully contained within the area of the retrieved
        map raster.

        .. note::
            If you know your camera will be nadir-facing, disabling ``map_update.gimbal_projection`` may improve
            performance by a small amount.
        """
        gimbal_projection_flag = self.get_parameter('map_update.gimbal_projection').get_parameter_value().bool_value
        if type(gimbal_projection_flag) is bool:
            return gimbal_projection_flag
        else:
            # Default behavior (safer)
            self.get_logger().warn(f'Could not read gimbal projection flag: {gimbal_projection_flag}. Assume False.')
            return False

    @property
    def _map_size_with_padding(self) -> Optional[Tuple[int, int]]:
        """Padded map size tuple (height, width) or None if the information is not available.

        Because the deep learning models used for predicting matching keypoints or poses between camera image frames
        and map rasters are not assumed to be rotation invariant in general, the map rasters are rotated based on
        camera yaw so that they align with the camera images. To keep the scale of the map after rotation the same,
        black corners would appear unless padding is used. Retrieved maps therefore have to be squares with the side
        lengths matching the diagonal of the camera frames so that scale is preserved and no black corners appear in
        the map rasters after rotation. The height and width will both be equal to the diagonal of the declared
        (:py:attr:`._bridge.camera_data`) camera frame dimensions.
        """
        if self._img_dim is None:
            self.get_logger().warn(f'Dimensions not available - returning None as map size.')
            return None
        diagonal = math.ceil(math.sqrt(self._img_dim.width ** 2 + self._img_dim.height ** 2))
        assert_type(diagonal, int)
        return diagonal, diagonal

    @property
    def _img_dim(self) -> Optional[Dim]:
        """Image resolution from latest :class:`px4_msgs.msg.CameraInfo` message, None if not available"""
        if self._bridge.camera_data is not None:
            return self._bridge.camera_data.dim
        else:
            self.get_logger().warn('Camera data was not available, returning None as declared image size.')
            return None

    @property
    def _vehicle_position(self) -> Optional[Position]:
        """Vehicle position guess in WGS84 coordinates and altitude in meters above ground, None if not available"""
        if self._bridge.global_position is not None:
            assert_type(self._bridge.global_position, get_args(Optional[GeoPoint]))

            crs = 'epsg:4326'
            if self._bridge.attitude is None:
                self.get_logger().warn('Vehicle attitude not yet available, cannot determine vehicle Position.')
                return None

            try:
                position = Position(
                    xy=self._bridge.global_position,
                    altitude=Altitude(
                        agl=self._bridge.altitude_agl(self._terrain_altitude_amsl_at_position(self._bridge.global_position)),
                        amsl=self._bridge.altitude_amsl,
                        ellipsoid=self._bridge.altitude_ellipsoid,
                        home=self._bridge.altitude_home
                    ),
                    attitude=self._bridge.attitude,
                    timestamp=self._bridge.synchronized_time
                )
                return position
            except DataValueError as dve:
                self.get_logger().warn(f'Error determining vehicle position:\n{dve},\n{traceback.print_exc()}.')
                return None
        else:
            return None

    @property
    def _estimation_inputs(self) -> Optional[Tuple[InputData, ContextualMapData]]:
        """Returns snapshot of vehicle state and reference map data required for pose estimation

        Pose estimation is asynchronous, so this property provides a way of taking snapshot of the required input
        arguments to :meth:`._estimate`.
        """
        input_data = InputData(
            r_guess=self._r_guess,
            snapshot=self._bridge.snapshot
        )

        static_camera = self.get_parameter('misc.static_camera').get_parameter_value().bool_value

        # Get cropped and rotated map
        if self._bridge.gimbal_set_attitude is not None and not static_camera:
            camera_yaw = self._bridge.gimbal_set_attitude.yaw
            assert_type(camera_yaw, float)
            assert -np.pi <= camera_yaw <= np.pi, f'Unexpected gimbal yaw value: {camera_yaw} ([-pi, pi] expected).'
        else:
            static_camera = self.get_parameter('misc.static_camera').get_parameter_value().bool_value
            if not static_camera:
                self.get_logger().warn(f'Camera yaw unknown, cannot estimate pose.')
                return
            else:
                self.get_logger().debug(f'Assuming zero yaw relative to vehicle body for static nadir-facing camera.')
                assert self._bridge.attitude is not None and hasattr(self._bridge.attitude, 'yaw')
                camera_yaw = self._bridge.attitude.yaw

        contextual_map_data = ContextualMapData(rotation=camera_yaw, map_data=self._map_data, crop=self._img_dim,
            altitude_scaling=self._altitude_scaling)
        return input_data, contextual_map_data
    # endregion

    # region Initialization
    def __declare_ros_params(self) -> None:
        """Declares ROS parameters"""
        # Need to declare parameters one by one since declare_parameters will not declare remaining parameters if it
        # raises a ParameterAlreadyDeclaredException
        for param_tuple in self._ROS_PARAMS:
            try:
                self.declare_parameter(*param_tuple)
                self.get_logger().debug(f'Using default value {param_tuple[1]} for ROS parameter {param_tuple[0]}')
            except rclpy.exceptions.ParameterAlreadyDeclaredException as _:
                # This means parameter is declared from YAML file
                pass

    def _setup_pose_estimation_pool(self, params_file: str) -> Tuple[type, Pool]:
        """Returns the pose estimator type along with an initialized pool

        :param params_file: Parameter file with pose estimator full class path and initialization arguments
        :return: Tuple containing the class type and the initialized pose estimation pool
        """
        # Use 'spawn', see: https://pytorch.org/docs/stable/notes/multiprocessing.html#cuda-in-multiprocessing
        try:
            torch.multiprocessing.set_start_method('spawn')
        except RuntimeError as _:
            # context has already been set
            pass
        pose_estimator_params = self._load_config(params_file)
        module_name, class_name = pose_estimator_params.get('class_name', '').rsplit('.', 1)
        pose_estimator = self._import_class(class_name, module_name)
        pose_estimator_pool = Pool(self._MATCHER_PROCESS_COUNT, initializer=pose_estimator.initializer,
                                   initargs=(pose_estimator, *pose_estimator_params.get('args', []),))  # TODO: handle missing args, do not use default value

        return pose_estimator, pose_estimator_pool

    def _load_config(self, yaml_file: str) -> dict:
        """Loads config from the provided YAML file

        :param yaml_file: Path to the yaml file
        :return: The loaded yaml file as dictionary
        """
        assert_type(yaml_file, str)
        with open(os.path.join(self._package_share_dir, yaml_file), 'r') as f:
            # noinspection PyBroadException
            try:
                config = yaml.safe_load(f)
                self.get_logger().info(f'Loaded config:\n{config}.')
                return config
            except Exception as e:
                self.get_logger().error(f'Could not load config file {yaml_file} because of unexpected exception.')
                raise

    def _setup_map_update_timer(self) -> rclpy.timer.Timer:
        """Sets up a timer to throttle map update requests

        Initially map updates were triggered in VehicleGlobalPosition message callbacks, but were moved to a separate
        timer since map updates may be needed even if the EKF2 filter does not publish a global position reference (e.g.
        when GPS fusion is turned off in the EKF2_AID_MASK parameter).

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

    def request_dem(self, map_candidate):
        """Request map for local frame origin to fix DEM origin"""

        self._dem_req = GetMap.Request()
        bbox = BBox(*map_candidate.bounds)
        bbox = BoundingBox(min_pt=ROSGeoPoint(latitude=bbox.bottom, longitude=bbox.left, altitude=np.nan),
                           max_pt=ROSGeoPoint(latitude=bbox.top, longitude=bbox.right, altitude=np.nan))
        self._dem_req.bbox = bbox
        if self._map_size_with_padding is None:
            self.get_logger().warn(f'Map size unknown, skipping requesting DEM.')
            return None
        self._dem_req.height, self._dem_req.width = self._map_size_with_padding
        self.get_logger().info(f'Requesting DEM for local frame origin location {bbox}.')
        self._dem_req_future = self._map_cli.call_async(self._dem_req)
        #rclpy.spin_until_future_complete(self, self._dem_req_future)
        #return self._dem_req_future.result()
        return self._dem_req_future

    def _map_update_timer_callback(self) -> None:
        """Attempts to update the stored map at regular intervals

        Also gets DEM for local frame origin if needed (see :meth:`._should_request_dem_

        Calls :meth:`._update_map` if the center and altitude coordinates for the new map raster are available and the
        :meth:`._should_update_map` check passes.

        New map is retrieved based on a guess of the vehicle's global position. If
        :py:attr:`._is_gimbal_projection_enabled`, the center of the projected camera field of view is used instead of
        vehicle position to ensure the field of view is best contained in the new map raster.
        """
        if self._should_request_dem_for_local_frame_origin() and self._map_cli.service_is_ready():
            if self._dem_req_future is not None and self._dem_req_future.done():
                # ASync call has returned
                if self._dem is None:
                    result = self._dem_req_future.result()
                    #self.get_logger().info(f'DEM result for local frame origin: {result}')
                    img = self._cv_bridge.imgmsg_to_cv2(result.image.img, desired_encoding='passthrough')
                    dem = self._cv_bridge.imgmsg_to_cv2(result.image.dem, desired_encoding='passthrough')
                    # dem = self._cv_bridge.imgmsg_to_cv2(result.dem, desired_encoding='mono8')  # TODO mono16? if more than 255 meters
                    assert self._dem_request_map_candidate is not None
                    self._dem = MapData(bbox=BBox(*self._dem_request_map_candidate.bounds), image=Img(img), elevation=Img(dem))

                    # TODO: assumes that this local_frame_origin is the starting location, same that was used for the request
                    #  --> not strictly true even if it works for the simulation
                    if self._origin_dem_altitude is None:
                        local_frame_origin = self._bridge.local_frame_origin
                        if local_frame_origin is not None:
                            self._origin_dem_altitude = self._terrain_altitude_at_position(local_frame_origin.xy,
                                                                                           local_origin=True)
            else:
                # Request DEM for local frame origin
                assert self._bridge.local_frame_origin is not None
                map_radius = self._get_dynamic_map_radius(self._DEM_REQUEST_ALTITUDE)
                map_candidate = GeoSquare(self._bridge.local_frame_origin.xy, map_radius)
                self.get_logger().info(f'Requesting DEM for local frame origin...')
                self.request_dem(map_candidate)
                self._dem_request_map_candidate = map_candidate

        if self._vehicle_position is None:
            self.get_logger().warn(f'Could not determine vehicle approximate global position and therefore cannot '
                                   f'update map.')
            return

        if self._is_gimbal_projection_enabled:
            projected_center = self._guess_fov_center(self._vehicle_position)
            if projected_center is None:
                self.get_logger().warn('Could not project field of view center. Using vehicle position for map center '
                                       'instead.')
        else:
            projected_center = None

        map_update_altitude = self._bridge.altitude_agl(self._terrain_altitude_amsl_at_position(self._bridge.global_position))
        if map_update_altitude is None:
            self.get_logger().warn('Cannot determine altitude AGL, skipping map update.')
            return None
        if map_update_altitude <= 0:
            self.get_logger().warn(f'Map update altitude {map_update_altitude} should be > 0, skipping map update.')
            return None
        map_radius = self._get_dynamic_map_radius(map_update_altitude)
        map_candidate = GeoSquare(projected_center if projected_center is not None else self._vehicle_position.xy,
                                  map_radius)

        # TODO: redundant with request_dem contents
        bbox = BBox(*map_candidate.bounds)
        bbox = BoundingBox(min_pt=ROSGeoPoint(latitude=bbox.bottom, longitude=bbox.left, altitude=np.nan),
                           max_pt=ROSGeoPoint(latitude=bbox.top, longitude=bbox.right, altitude=np.nan))
        self._bbox_pub.publish(bbox)

    def _import_class(self, class_name: str, module_name: str) -> type:
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

    def _load_autopilot(self, autopilot: str) -> Callable:
        """Returns :class:`.Autopilot` instance from provided class path

        :param autopilot: Full path of :class:`.Autopilot` adapter class
        :return: Initialized :class:`.Autopilot` instance
        """
        assert_type(autopilot, str)
        module_name, class_name = autopilot.rsplit('.', 1)
        class_ = self._import_class(class_name, module_name)
        return class_
    # endregion

    #region WMSWorkerCallbacks
    def _orthoimage3d_callback(self, msg: OrthoImage3D) -> None:
        """Callback for :class:`.OrthoImage3D` message"""
        if self._msg != msg:  # TODO: add property for _msg
            self._msg = msg
            # TODO: also in map callback
            img = self._cv_bridge.imgmsg_to_cv2(msg.img, desired_encoding='passthrough')
            dem = self._cv_bridge.imgmsg_to_cv2(msg.dem, desired_encoding='passthrough')
            #dem = self._cv_bridge.imgmsg_to_cv2(msg.dem, desired_encoding='mono8')  # TODO mono16? if more than 255 meters
            bbox = BBox(msg.bbox.min_pt.longitude, msg.bbox.min_pt.latitude, msg.bbox.max_pt.longitude,
                        msg.bbox.max_pt.latitude)  # TODO: have method that converts BoudningBox<->BBox
            self._wms_pool_worker_callback(bbox, (img, dem))

    def _wms_pool_worker_callback(self, bbox, result: Tuple[np.ndarray, Optional[np.ndarray]]) -> None:
        """Handles result from :meth:`gisnav.wms.worker`.

        Saves received result to :py:attr:`~_map_data. The result should be a collection containing a single
        :class:`~data.MapData`.

        .. note::
            The result could be either from a regular map update request, or it could be a DEM for local frame origin

        :param result: Results from the asynchronous call
        :return:
        """
        map_, elevation = result
        assert_type(map_, np.ndarray)
        if elevation is not None:
            assert_ndim(elevation, 2)
            assert_shape(elevation, map_.shape[0:2])

        # Should already have received camera info so _map_size_with_padding should not be None
        assert map_.shape[0:2] == self._map_size_with_padding, f'Decoded map {map_.shape[0:2]} is not of specified ' \
                                                               f'size {self._map_size_with_padding}.'

        elevation = Img(elevation) if elevation is not None else None
        map_data = MapData(bbox=bbox, image=Img(map_), elevation=elevation)
        self.get_logger().info(f'Map received for bbox: {map_data.bbox}.')

        self._map_data = map_data

    def _wms_pool_worker_error_callback(self, e: BaseException) -> None:
        """Handles errors from WMS pool worker.

        :param e: Exception returned by the worker
        :return:
        """
        # TODO: handle IOError separately?
        # These are *probably* connection related exceptions from requests library. They do not seem to be part of
        # OWSLib public API so WMSClient does not handle them (in case OWSLib devs change it). Handling them would
        # require direct dependency to requests. Log exception as error here and move on.
        self.get_logger().error(f'Something went wrong with WMS process:\n{e},\n{traceback.print_exc()}.')
    #endregion

    def _image_raw_callback(self, msg: Image) -> None:
        """Handles latest :class:`px4_msgs.msg.Image` message

        :param msg: The :class:`px4_msgs.msg.Image` message from the PX4-ROS 2 bridge
        """
        # Estimate EKF2 timestamp first to get best estimate
        if self._bridge.synchronized_time is None:
            self.get_logger().warn('Image frame received but could not estimate EKF2 system time, skipping frame.')
            return None

        assert_type(msg, Image)
        cv_image = self._cv_bridge.imgmsg_to_cv2(msg, self._IMAGE_ENCODING)

        # Check that image dimensions match declared dimensions
        if self._img_dim is not None:
            cv_img_shape = cv_image.shape[0:2]
            assert cv_img_shape == self._img_dim, f'Converted cv_image shape {cv_img_shape} did not match '\
                                                  f'declared image shape {self._img_dim}.'

        if self._bridge.camera_data is None:
            self.get_logger().warn('Camera data not yet available, skipping frame.')
            return None

        image_data = ImageData(image=Img(cv_image), frame_id=msg.header.frame_id,
                               timestamp=self._bridge.synchronized_time, camera_data=self._bridge.camera_data)

        if self._should_estimate(image_data.image.arr):
            assert self._pose_estimation_query is None or self._pose_estimation_query.result.ready()
            assert self._map_data is not None
            assert hasattr(self._map_data, 'image'), 'Map data unexpectedly did not contain the image data.'

            inputs, contextual_map_data = self._estimation_inputs
            self._map_input_data = inputs
            image_pair = ImagePair(image_data, contextual_map_data)
            self._estimate(image_pair, inputs)

    # region Map Updates
    def _terrain_altitude_at_position(self, position: Optional[GeoPoint], local_origin: bool = False) -> Optional[float]:
        """Raw terrain altitude from DEM if available, or None if not available

        :param position: Position to query
        :param local_origin: True to use :py:attr:`._dem` (retrieved specifically for local frame origin)
        :return: Raw altitude in DEM coordinate frame and units
        """
        map_data = self._map_data if not local_origin else self._dem
        if map_data is not None and position is not None:
            elevation = map_data.elevation.arr
            bbox = map_data.bbox
            #polygon = bbox._geoseries[0]
            polygon = box(*bbox)
            # position = self._bridge.global_position
            point = position._geoseries[0]

            if polygon.contains(point):
                h, w = elevation.shape[0:2]
                assert h, w == self._img_dim
                #left, bottom, right, top = bbox.bounds
                left, bottom, right, top = bbox
                x = w * (position.lon - left) / (right - left)
                y = h * (position.lat - bottom) / (top - bottom)
                try:
                    dem_elevation = elevation[int(np.floor(y)), int(np.floor(x))]
                except IndexError as _:
                    # TODO: might be able to handle this
                    self.get_logger().warn('Position seems to be outside current elevation raster, cannot compute '
                                           'terrain altitude.')
                    return None

                return float(dem_elevation)
            else:
                # Should not happen
                self.get_logger().warn('Did not have elevation raster for current location or local frame origin '
                                       'altitude was unknwon, cannot compute terrain altitude.')
                return None

        self.get_logger().warn(f'Map data or position not provided, cannot determine DEM elevation at position '
                               f'{position.latlon}.')
        return None

    def _terrain_altitude_amsl_at_position(self, position: Optional[GeoPoint]):
        """Terrain altitude in meters AMSL accroding to DEM if available, or None if not available

        :param position: Position to query
        :return: Terrain altitude AMSL in meters at position
        """
        dem_elevation = self._terrain_altitude_at_position(position)
        local_frame_origin = self._bridge.local_frame_origin
        if dem_elevation is not None and self._origin_dem_altitude is not None and local_frame_origin is not None:
            elevation_relative = dem_elevation - self._origin_dem_altitude
            elevation_amsl = elevation_relative + local_frame_origin.altitude.amsl
            return float(elevation_amsl)

        return None

    def _guess_fov_center(self, origin: Position) -> Optional[GeoPoint]:
        """Guesses WGS84 coordinates of camera field of view (FOV) projected on ground from given origin

        Triggered by :meth:`._map_update_timer_callback` when gimbal projection is enabled to determine center
        coordinates for next WMS GetMap request.

        :param origin: Camera position
        :return: Center of the projected FOV, or None if not available
        """
        static_camera = self.get_parameter('misc.static_camera').get_parameter_value().bool_value

        if self._bridge.gimbal_set_attitude is None:
            if not static_camera:
                self.get_logger().warn('Gimbal set attitude not available, cannot project gimbal FOV.')
                return None
            else:
                if self._bridge.attitude is not None:
                    attitude = self._bridge.attitude.as_rotation()
                    attitude *= Rotation.from_euler('XYZ', [0, -np.pi/2, 0])
                    gimbal_set_attitude = Attitude(attitude.as_quat()).to_esd().r
                else:
                    self.get_logger().warn('Vehicle attitude not available, will not provide pose guess for static '
                                           'camera.')
                    return None
        else:
            assert_type(self._bridge.gimbal_set_attitude, Attitude)
            gimbal_set_attitude = self._bridge.gimbal_set_attitude.to_esd()  # Need coordinates in image frame, not NED

        assert gimbal_set_attitude is not None

        if self._bridge.camera_data is None:
            self.get_logger().warn('Camera data not available, could not create a mock pose to generate a FOV guess.')
            return None

        translation = -gimbal_set_attitude.r @ np.array([self._bridge.camera_data.cx, self._bridge.camera_data.cy,
                                                         -self._bridge.camera_data.fx])

        try:
            pose = Pose(gimbal_set_attitude.r, translation.reshape((3, 1)))
        except DataValueError as e:
            self.get_logger().warn(f'Pose input values: {gimbal_set_attitude.r}, {translation} were invalid: {e}.')
            return None

        try:
            mock_fixed_camera = FixedCamera(pose=pose, image_pair=self._mock_image_pair(origin),
                                            snapshot=self._bridge.snapshot,
                                            timestamp=self._bridge.synchronized_time)
        except DataValueError as _:
            self.get_logger().warn(f'Could not create a valid mock projection of FOV.')
            return None

        if __debug__:
            export_projection = self.get_parameter('debug.export_projection').get_parameter_value().string_value
            if export_projection != '':
                self._export_position(mock_fixed_camera.fov.c, mock_fixed_camera.fov.fov, export_projection)

        return mock_fixed_camera.fov.fov.to_crs('epsg:4326').center

    def _should_request_dem_for_local_frame_origin(self) -> bool:
        """Returns True if a new map should be requested to determine elevation value for local frame origin

        DEM value for local frame origin is needed if elevation layer is used in order to determine absolute altitude
        of altitude estimates (GISNav estimates altitude against DEM, or assumes altitude at 0 if no DEM is provided).

        :return: True if new map should be requested
        """
        if self._origin_dem_altitude is not None:
            self.get_logger().warn(f'Not requesting DEM because origin_dem_altitude is already set.')
            return False

        if self._bridge.local_frame_origin is None:
            self.get_logger().warn(f'Not requesting DEM because local_frame_origin is not available.')
            return False

        return True

    def _get_dynamic_map_radius(self, altitude: Union[int, float]) -> int:
        """Returns map radius that adjusts for camera altitude to be used for new map requests

        :param altitude: Altitude of camera in meters
        :return: Suitable map radius in meters
        """
        assert_type(altitude, get_args(Union[int, float]))
        max_map_radius = self.get_parameter('map_update.max_map_radius').get_parameter_value().integer_value

        if self._bridge.camera_data is not None:
            hfov = 2 * math.atan(self._bridge.camera_data.dim.width / (2 * self._bridge.camera_data.fx))
            map_radius = 1.5*hfov*altitude  # Arbitrary padding of 50%
        else:
            # Update map before CameraInfo has been received
            self.get_logger().warn(f'Could not get camera data, using guess for map width.')
            map_radius = 3*altitude  # Arbitrary guess

        if map_radius > max_map_radius:
            self.get_logger().warn(f'Dynamic map radius {map_radius} exceeds max map radius {max_map_radius}, using '
                                   f'max radius {max_map_radius} instead.')
            map_radius = max_map_radius

        return map_radius
    # endregion

    # region Mock Image Pair
    def _mock_image_pair(self, origin: Position) -> Optional[ImagePair]:
        """Creates mock :class:`.ImagePair` for guessing projected FOV needed for map requests, or None if not available

        The mock image pair will be paired with a pose guess to compute the expected field of view. The expected field
        of view is used to request a new map that overlaps with what the camera is looking at.

        .. seealso:
            :meth:`._mock_map_data` and :meth:`._mock_image_data`

        :param origin: Vehicle position
        :return: Mock image pair that can be paired with a pose guess to generate a FOV guess, or None if not available
        """
        image_data = self._mock_image_data()
        map_data = self._mock_map_data(origin)
        if image_data is None or map_data is None:
            self.get_logger().warn('Missing required inputs for generating mock image PAIR.')
            return None
        contextual_map_data = ContextualMapData(rotation=0, crop=image_data.image.dim, map_data=map_data,
                                                mock_data=True)
        image_pair = ImagePair(image_data, contextual_map_data)
        return image_pair

    # TODO: make property?
    def _mock_image_data(self) -> Optional[ImageData]:
        """Creates mock :class:`.ImageData` for guessing projected FOV for map requests, or None if not available

        .. seealso:
            :meth:`._mock_map_data` and :meth:`._mock_image_pair`
        """
        if self._img_dim is None or self._bridge.synchronized_time is None or self._bridge.camera_data is None:
            self.get_logger().warn('Missing required inputs for generating mock image DATA.')
            return None

        image_data = ImageData(image=Img(np.zeros(self._img_dim)),
                               frame_id='mock_image_data',  # TODO
                               timestamp=self._bridge.synchronized_time,
                               camera_data=self._bridge.camera_data)
        return image_data

    def _mock_map_data(self, origin: Position) -> Optional[MapData]:
        """Creates mock :class:`.MapData` for guessing projected FOV needed for map requests, or None if not available

        The mock image pair will be paired with a pose guess to compute the expected field of view. The expected field
        of view is used to request a new map that overlaps with what the camera is looking at.

        .. seealso:
            :meth:`._mock_image_pair` and :meth:`._mock_image_data`

        :param origin: Vehicle position
        :return: Mock map data with mock images but with real expected bbox, or None if not available
        """
        assert_type(origin, Position)
        if self._bridge.camera_data is None or self._map_size_with_padding is None:
            self.get_logger().warn('Missing required inputs for generating mock MAP DATA.')
            return None

        # Scaling factor of image pixels := terrain_altitude
        scaling = (self._map_size_with_padding[0]/2) / self._bridge.camera_data.fx
        altitude = self._bridge.altitude_agl(self._terrain_altitude_amsl_at_position(self._bridge.global_position))
        if altitude is None:
            self.get_logger().warn('Cannot determine altitude AGL, skipping mock map data.')
            return
        if altitude < 0:
            self.get_logger().warn(f'Altitude AGL {altitude} was negative, skipping mock map data.')
            return
        radius = scaling * altitude

        assert_type(origin.xy, GeoPoint)
        bbox = GeoSquare(origin.xy, radius)
        #map_data = MapData(bbox=bbox, image=Img(np.zeros(self._map_size_with_padding)))
        map_data = MapData(bbox=BBox(*bbox.bounds), image=Img(np.zeros(self._map_size_with_padding)))
        return map_data
    # endregion

    # region Pose Estimation Callbacks
    def _pose_estimation_worker_error_callback(self, e: BaseException) -> None:
        """Error callback for matching worker"""
        self.get_logger().error(f'Pose estimator encountered an unexpected exception:\n{e}\n{traceback.print_exc()}.')

    def _pose_estimation_worker_callback(self, result: Optional[Pose]) -> None:
        """Callback for :meth:`.PoseEstimator.worker`

        Retrieves latest :py:attr:`._pose_estimation_query.input_data` and uses it to call :meth:`._compute_output`.
        The input data is needed so that the post-processing is done using the same state information that was used for
        initiating the pose estimation in the first place. For example, camera pitch may have changed since then,
        and current camera pitch should therefore not be used for processing the matches.

        :param result: Pose result from WMS worker, or None if pose could not be estimated
        """
        if result is not None:
            try:
                pose = Pose(*result)

                if self._pose_estimation_query.image_pair.ref.elevation is not None:
                    # Compute DEM value at estimated position
                    # This is in camera intrinsic (pixel) units with origin at whatever the DEM uses
                    # For example, the USGS DEM uses NAVD 88
                    x, y = -pose.t.squeeze()[0:2]
                    x, y = int(x), int(y)
                    elevation = self._pose_estimation_query.image_pair.ref.elevation.arr[y, x]
                    pose = Pose(pose.r, pose.t - elevation)
            except DataValueError as _:
                self.get_logger().warn(f'Estimated pose was not valid, skipping this frame.')
                return None
            except IndexError as __:
                # TODO: might be able to handle this
                self.get_logger().warn(f'Estimated pose was not valid, skipping this frame.')
                return None

            self._pose_guess = pose
        else:
            self.get_logger().warn(f'Worker did not return a pose, skipping this frame.')
            return None

        try:
            image_pair = self._pose_estimation_query.image_pair
            input_data = self._pose_estimation_query.input_data
            #self.get_logger().info(f'snapshot terrain alt {input_data.snapshot.terrain_altitude}')
            fixed_camera = FixedCamera(pose=pose, image_pair=image_pair, snapshot=input_data.snapshot,
                                       timestamp=image_pair.qry.timestamp)
        except DataValueError as _:
            self.get_logger().warn(f'Could not estimate a valid camera position, skipping this frame.')
            return None

        if not self._is_valid_estimate(fixed_camera, self._pose_estimation_query.input_data):
            self.get_logger().warn('Estimate did not pass post-estimation validity check, skipping this frame.')
            return None

        assert fixed_camera is not None
        # noinspection PyUnreachableCode
        if __debug__:
            # Visualize projected FOV estimate
            fov_pix = fixed_camera.fov.fov_pix
            ref_img = fixed_camera.image_pair.ref.image.arr
            map_with_fov = cv2.polylines(ref_img.copy(),
                                         [np.int32(fov_pix)], True,
                                         255, 3, cv2.LINE_AA)

            img = np.vstack((map_with_fov, fixed_camera.image_pair.qry.image.arr))
            cv2.imshow("Projected FOV", img)
            cv2.waitKey(1)

            # Export GeoJSON
            export_geojson = self.get_parameter('debug.export_position').get_parameter_value().string_value
            if export_geojson != '':
                self._export_position(fixed_camera.position.xy, fixed_camera.fov.fov, export_geojson)

        self.publish(fixed_camera)

        self._map_input_data_prev = self._map_input_data
        self._fixed_camera_prev = fixed_camera
    # endregion

    # region Pose Estimation
    def _should_estimate(self, img: np.ndarray) -> bool:
        """Determines whether :meth:`._estimate` should be called

        Match should be attempted if (1) a reference map has been retrieved, (2) there are no pending match results,
        (3) camera roll or pitch is not too high (e.g. facing horizon instead of nadir), and (4) drone is not flying
        too low.

        :param img: Image from which to estimate pose
        :return: True if pose estimation be attempted
        """
        # Check condition (1) - that _map_data exists
        if self._map_data is None:
            self.get_logger().debug(f'No reference map available. Skipping pose estimation.')
            return False

        # Check condition (2) - that a request is not already running
        if not (self._pose_estimation_query is None or self._pose_estimation_query.result.ready()):
            self.get_logger().debug(f'Previous pose estimation results pending. Skipping pose estimation.')
            return False

        # Check condition (3) - whether camera roll/pitch is too large
        max_pitch = self.get_parameter('misc.max_pitch').get_parameter_value().integer_value
        if self._camera_roll_or_pitch_too_high(max_pitch):
            self.get_logger().warn(f'Camera roll or pitch not available or above limit {max_pitch}. Skipping pose '
                                   f'estimation.')
            return False

        # Check condition (4) - whether vehicle altitude is too low
        min_alt = self.get_parameter('misc.min_match_altitude').get_parameter_value().integer_value
        altitude = self._bridge.altitude_agl(self._terrain_altitude_amsl_at_position(self._bridge.global_position))
        if altitude is None:
            self.get_logger().warn('Cannot determine altitude AGL, skipping map update.')
            return None
        if not isinstance(min_alt, int) or altitude is None or altitude < min_alt:
            self.get_logger().warn(f'Assumed altitude {altitude} was lower than minimum threshold for matching '
                                   f'({min_alt}) or could not be determined. Skipping pose estimation.')
            return False

        return True

    def _estimate(self, image_pair: ImagePair, input_data: InputData) -> None:
        """Instructs the pose estimator to estimate the pose between the image pair

        :param image_pair: The image pair to estimate the pose from
        :param input_data: Input data context
        :return:
        """
        assert self._pose_estimation_query is None or self._pose_estimation_query.result.ready()
        pose_guess = None if self._pose_guess is None else tuple(self._pose_guess)

        # Scale elevation raster from meters to camera native pixels
        elevation = image_pair.ref.elevation.arr if image_pair.ref.elevation is not None else None
        if elevation is not None:
            elevation = elevation * image_pair.ref.altitude_scaling if image_pair.ref.altitude_scaling is not None \
                else None

        self._pose_estimation_query = AsyncPoseQuery(
            result=self._pose_estimator_pool.apply_async(
                self._pose_estimator.worker,
                (image_pair.qry.image.arr, image_pair.ref.image.arr, image_pair.qry.camera_data.k, pose_guess,
                 elevation),
                callback=self._pose_estimation_worker_callback,
                error_callback=self._pose_estimation_worker_error_callback
            ),
            image_pair=image_pair,
            input_data=input_data
        )

    def _is_valid_estimate(self, fixed_camera: FixedCamera, input_data: InputData) -> bool:
        """Returns True if the estimate is valid

        Compares computed estimate to guess based on set gimbal device attitude. This will reject estimates made when
        the gimbal was not stable (which is strictly not necessary), which is assumed to filter out more inaccurate
        estimates.
        """
        static_camera = self.get_parameter('misc.static_camera').get_parameter_value().bool_value
        if static_camera:
            vehicle_attitude = self._bridge.attitude
            if vehicle_attitude is None:
                self.get_logger().warn('Gimbal attitude was not available, cannot do post-estimation validity check'
                                       'for static camera.')
                return False

            # Add vehicle roll & pitch (yaw handled separately through map rotation)
            #r_guess = Attitude(Rotation.from_rotvec([vehicle_attitude.roll, vehicle_attitude.pitch - np.pi/2, 0])
            #                   .as_quat()).to_esd().as_rotation()
            r_guess = Attitude(Rotation.from_euler('XYZ', [vehicle_attitude.roll, vehicle_attitude.pitch - np.pi / 2,
                                                           0]).as_quat()).to_esd().as_rotation()

        if input_data.r_guess is None and not static_camera:
            self.get_logger().warn('Gimbal attitude was not available, cannot do post-estimation validity check.')
            return False

        if not static_camera:
            r_guess = Rotation.from_matrix(input_data.r_guess)
            # Adjust for map rotation
            camera_yaw = fixed_camera.image_pair.ref.rotation
            camera_yaw = Rotation.from_euler('xyz', [0, 0, camera_yaw], degrees=False)
            r_guess *= camera_yaw

        r_estimate = Rotation.from_matrix(fixed_camera.pose.r)

        magnitude = Rotation.magnitude(r_estimate * r_guess.inv())

        threshold = self.get_parameter('misc.attitude_deviation_threshold').get_parameter_value().integer_value
        threshold = np.radians(threshold)

        if magnitude > threshold:
            self.get_logger().warn(f'Estimated rotation difference to expected was too high (magnitude '
                                   f'{np.degrees(magnitude)}).')
            return False

        #roll = r_guess.as_euler('xyz', degrees=True)[0]
        #if roll > threshold/2:  # TODO: have separate configurable threshold
        #    self.get_logger().warn(f'Estimated roll difference to expected was too high (magnitude '
        #                           f'{roll}).')
        #    return False

        return True


    # endregion

    # region Shared Logic
    def _camera_roll_or_pitch_too_high(self, max_pitch: Union[int, float]) -> bool:
        """Returns True if (set) camera roll or pitch exceeds given limit OR camera pitch is unknown

        Used to determine whether camera roll or pitch is too high up from nadir to make matching against a map
        not worthwhile. Checks roll for static camera, but assumes zero roll for 2-axis gimbal (static_camera: False).

        .. note::
            Uses actual vehicle attitude (instead of gimbal set attitude) if static_camera ROS param is True

        :param max_pitch: The limit for the pitch in degrees from nadir over which it will be considered too high
        :return: True if pitch is too high
        """
        assert_type(max_pitch, get_args(Union[int, float]))
        static_camera = self.get_parameter('misc.static_camera').get_parameter_value().bool_value
        pitch = None
        if self._bridge.gimbal_set_attitude is not None and not static_camera:
            # TODO: do not assume zero roll here - camera attitude handling needs refactoring
            # +90 degrees to re-center from FRD frame to nadir-facing camera as origin for max pitch comparison
            pitch = np.degrees(self._bridge.gimbal_set_attitude.pitch) + 90
        else:
            if not static_camera:
                self.get_logger().warn('Gimbal attitude was not available, assuming camera pitch too high.')
                return True
            else:
                if self._bridge.attitude is None:
                    self.get_logger().warn('Vehicle attitude was not available, assuming static camera pitch too high.')
                    return True
                else:
                    pitch = max(self._bridge.attitude.pitch, self._bridge.attitude.roll)

        assert pitch is not None
        if pitch > max_pitch:
            self.get_logger().warn(f'Camera pitch {pitch} is above limit {max_pitch}.')
            return True

        return False

    def _export_position(self, position: GeoPoint, fov: GeoTrapezoid, filename: str) -> None:
        """Exports the computed position and field of view (FOV) into a geojson file

        The GeoJSON file is not used by the node but can be accessed by GIS software to visualize the data it contains.

        :param position: Computed camera position or projected principal point for gimbal projection
        :param: fov: Field of view of camera
        :param filename: Name of file to write into
        :return:
        """
        assert_type(position, GeoPoint)
        assert_type(fov, GeoTrapezoid)
        assert_type(filename, str)
        try:
            position._geoseries.append(fov._geoseries).to_file(filename)
        except Exception as e:
            self.get_logger().error(f'Could not write file {filename} because of exception:'
                                    f'\n{e}\n{traceback.print_exc()}')
    # endregion

    # region PublicAPI
    @abstractmethod
    def publish(self, position: Position) -> None:
        """Publishes the estimated position

        This method should be implemented by the extending class to adapt the base node for any given use case.

        :param position: Visually estimated position and attitude
        """
        pass

    def terminate_pools(self) -> None:
        """Terminates the WMS and pose estimator pools

        .. note::
            Call this method after :meth:`.destroy_timers` and before destroying your node and shutting down for a
            clean exit.
        """
        if self._pose_estimator_pool is not None:
            self.get_logger().info('Terminating pose estimator pool.')
            self._pose_estimator_pool.terminate()

    def destroy_timers(self) -> None:
        """Destroys the map update timer

        .. note::
            Call this method before destroying your node and before :meth:`.terminate_pools` and shutting down for a
            clean exit.
        """
        if self._map_update_timer is not None:
            self.get_logger().info('Destroying map update timer.')
            self._map_update_timer.destroy()

    def unsubscribe_topics(self) -> None:
        """Unsubscribes from all ROS topics

        .. note::
            Call this method when before destroying your node for a clean exit.
        """
        self._bridge.unsubscribe_all()
    # endregion
