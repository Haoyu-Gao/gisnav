import rclpy
import sys
import os
import traceback
import yaml
import importlib
import math
import time

from enum import Enum
from rclpy.node import Node
from rcl_interfaces.msg import ParameterDescriptor
from owslib.wms import WebMapService
from cv2 import VideoCapture, imwrite, imdecode
import numpy as np
import cv2
from cv_bridge import CvBridge
from scipy.spatial.transform import Rotation
from functools import partial
from wms_map_matching.util import get_bbox, setup_sys_path, convert_fov_from_pix_to_wgs84,\
    write_fov_and_camera_location_to_geojson, get_bbox_center, BBox, Dimensions, rotate_and_crop_map, \
    visualize_homography, get_fov, get_camera_distance, get_distance_of_fov_center, LatLon, fov_to_bbox,\
    get_angle, create_src_corners, uncrop_pixel_coordinates, rotate_point, move_distance, RPY, LatLonAlt, distances,\
    ImageFrameStamp, distance

# Add the share folder to Python path
share_dir, superglue_dir = setup_sys_path()

# Import this after util.setup_sys_path has been called
from wms_map_matching.superglue import SuperGlue


class Matcher(Node):
    # scipy Rotations: {‘X’, ‘Y’, ‘Z’} for intrinsic, {‘x’, ‘y’, ‘z’} for extrinsic rotations
    EULER_SEQUENCE = 'YXZ'

    # Minimum matches for homography estimation, should be at least 4
    MINIMUM_MATCHES = 4

    # Encoding of input video (input to CvBridge)
    IMAGE_ENCODING = 'bgr8'  # E.g. gscam2 only supports bgr8 so this is used to override encoding in image header

    # Local frame reference for px4_msgs.msg.VehicleVisualOdometry messages
    LOCAL_FRAME_NED = 0

    class TopicType(Enum):
        """Enumerates microRTPS bridge topic types."""
        PUB = 1
        SUB = 2

    def __init__(self, share_directory, superglue_directory, config='config.yml'):
        """Initializes the node.

        Arguments:
            share_dir - String path of the share directory where configuration and other files are.
            superglue_dir - String path of the directory where SuperGlue related files are.
            config - String path to the config file in the share folder.
        """
        super().__init__('matcher')
        self.share_dir = share_directory  # TODO: make private?
        self.superglue_dir = superglue_directory  # TODO: move this to _setup_superglue? private _superglue_dir instead?
        self._load_config(config)
        self._init_wms()

        # Dict for storing all microRTPS bridge subscribers and publishers
        self._topics = dict()
        self._setup_topics()

        # Dict for storing latest microRTPS messages
        self._topics_msgs = dict()

        # Converts image_raw to cv2 compatible image
        self._cv_bridge = CvBridge()

        # Store map raster received from WMS endpoint here along with its bounding box
        self._map = None  # TODO: put these together in a MapBBox structure to make them 'more atomic' (e.g. ImageFrameStamp named tuple)
        self._map_bbox = None

        self._setup_superglue()

        self._gimbal_fov_wgs84 = []  # TODO: remove this attribute, just passing it through here from _update_map to _match (temp hack)

        # Store previous image frame information for computing velocity in local frame
        self._previous_image_frame_stamp = None

        # To be used for pyproj transformations
        #self._g = pyproj.Geod(ellps='clrk66')  # TODO: move pyproj stuff from util.py here under Matcher() class

    def _setup_superglue(self):
        """Sets up SuperGlue."""  # TODO: make all these private?
        self._superglue = SuperGlue(self._config['superglue'], self.get_logger())

    def _get_previous_global_position(self):
        """Returns previous global position (WGS84)."""
        raise NotImplementedError

    def _load_config(self, yaml_file):
        """Loads config from the provided YAML file."""
        with open(os.path.join(share_dir, yaml_file), 'r') as f:
            try:
                self._config = yaml.safe_load(f)
                self.get_logger().info('Loaded config:\n{}.'.format(self._config))
            except Exception as e:
                self.get_logger().error('Could not load config file {} because of exception: {}\n{}' \
                                        .format(yaml_file, e, traceback.print_exc()))

    def _use_gimbal_projection(self):
        """Returns True if gimbal projection is enabled for fetching map bbox rasters."""
        gimbal_projection_flag = self._config.get('misc', {}).get('gimbal_projection', False)
        if type(gimbal_projection_flag) is bool:
            return gimbal_projection_flag
        else:
            self.get_logger().warn(f'Could not read gimbal projection flag: {gimbal_projection_flag}. Assume False.')
            return False

    def _restrict_affine(self):
        """Returns True if homography matrix should be restricted to an affine transformation (nadir facing camera)."""
        restrict_affine_flag = self._config.get('misc', {}).get('affine', False)
        if type(restrict_affine_flag) is bool:
            return restrict_affine_flag
        else:
            self.get_logger().warn(f'Could not read affine restriction flag: {restrict_affine_flag}. Assume False.')
            return False

    def _import_class(self, class_name, module_name):
        """Imports class from module if not yet imported."""
        if module_name not in sys.modules:
            self.get_logger().info('Importing module ' + module_name + '.')
            importlib.import_module(module_name)
        imported_class = getattr(sys.modules[module_name], class_name, None)
        assert imported_class is not None, class_name + ' was not found in module ' + module_name + '.'
        return imported_class

    def _setup_topics(self):
        """Loads and sets up ROS2 publishers and subscribers from config file."""
        for topic_name, msg_type in self._config['ros2_topics']['sub'].items():
            module_name, msg_type = msg_type.rsplit('.', 1)
            msg_class = self._import_class(msg_type, module_name)
            self._init_topic(topic_name, self.TopicType.SUB, msg_class)

        for topic_name, msg_type in self._config['ros2_topics']['pub'].items():
            module_name, msg_type = msg_type.rsplit('.', 1)
            msg_class = self._import_class(msg_type, module_name)
            self._init_topic(topic_name, self.TopicType.PUB, msg_class)

        self.get_logger().info('Topics setup complete with keys: ' + str(self._topics.keys()))

    def _init_topic(self, topic_name, topic_type, msg_type):
        """Sets up rclpy publishers and subscribers and dynamically loads message types from px4_msgs library."""
        if topic_type is self.TopicType.PUB:
            self._topics[topic_name] = self.create_publisher(msg_type, topic_name, 10)
        elif topic_type is self.TopicType.SUB:
            callback_name = '_' + topic_name.lower() + '_callback'
            callback = getattr(self, callback_name, None)
            assert callback is not None, 'Missing callback implementation: ' + callback_name
            self._topics[topic_name] = self.create_subscription(msg_type, topic_name, callback, 10)
        else:
            raise TypeError('Unsupported topic type: {}'.format(topic_type))

    def _init_wms(self):
        """Initializes the Web Map Service (WMS) client used by the node to request map rasters.

        The url and version parameters are required to initialize the WMS client and are therefore set to read only. The
        layer and srs parameters can be changed dynamically.
        """
        self.declare_parameter('url', self._config['wms']['url'], ParameterDescriptor(read_only=True))
        self.declare_parameter('version', self._config['wms']['version'], ParameterDescriptor(read_only=True))
        self.declare_parameter('layer', self._config['wms']['layer'])
        self.declare_parameter('srs', self._config['wms']['srs'])

        try:
            self._wms = WebMapService(self.get_parameter('url').get_parameter_value().string_value,
                                      version=self.get_parameter('version').get_parameter_value().string_value)
        except Exception as e:
            self.get_logger().error('Could not connect to WMS server.')
            raise e

    def _map_size(self):
        max_dim = max(self._camera_info().width, self._camera_info().height)
        return max_dim, max_dim

    def _camera_info(self):
        """Returns camera info."""
        return self._get_simple_info('camera_info')

    def _get_simple_info(self, message_name):
        """Returns message received via microRTPS bridge or None if message was not yet received."""
        info = self._topics_msgs.get(message_name, None)
        if info is None:
            self.get_logger().warn(message_name + ' info not available.')
        return info

    def _global_position(self):
        return self._get_simple_info('VehicleGlobalPosition')

    def _local_position(self):
        return self._get_simple_info('VehicleLocalPosition')

    def _map_size_with_padding(self):
        dim = self._img_dimensions()
        if type(dim) is not Dimensions:
            self.get_logger().warn(f'Dimensions not available - returning None as map size.')
            return None
        assert hasattr(dim, 'width') and hasattr(dim, 'height'), 'Dimensions did not have expected attributes.'
        diagonal = math.ceil(math.sqrt(dim.width ** 2 + dim.height ** 2))
        return diagonal, diagonal

    def _map_dimensions_with_padding(self):
        map_size = self._map_size_with_padding()
        if map_size is None:
            self.get_logger().warn(f'Map size with padding not available - returning None as map dimensions.')
            return None
        assert len(map_size) == 2, f'Map size was unexpected length {len(map_size)}, 2 expected.'
        return Dimensions(*map_size)

    def _declared_img_size(self):
        camera_info = self._camera_info()
        if camera_info is not None:
            assert hasattr(camera_info, 'height') and hasattr(camera_info, 'width'), \
                'Height or width info was unexpectedly not included in CameraInfo message.'
            return camera_info.height, camera_info.width  # numpy order: h, w, c --> height first
        else:
            self.get_logger().warn('Camera info was not available - returning None as declared image size.')
            return None

    def _img_dimensions(self):
        declared_size = self._declared_img_size()
        if declared_size is None:
            self.get_logger().warn('CDeclared size not available - returning None as image dimensions.')
            return None
        else:
            return Dimensions(*declared_size)

    def _vehicle_attitude(self):
        """Returns vehicle attitude from VehicleAttitude message."""
        return self._get_simple_info('VehicleAttitude')

    def _project_gimbal_fov(self, altitude_meters):
        """Returns field of view BBox projected using gimbal attitude and camera intrinsics information."""
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Could not get RPY - cannot project gimbal fov.')
            return

        r = Rotation.from_euler(self.EULER_SEQUENCE, list(rpy), degrees=True).as_matrix()
        e = np.hstack((r, np.expand_dims(np.array([0, 0, altitude_meters]), axis=1)))  # extrinsic matrix  # [0, 0, 1]
        assert e.shape == (3, 4), 'Extrinsic matrix had unexpected shape: ' + str(e.shape) \
                                  + ' - could not project gimbal FoV.'

        camera_info = self._camera_info()
        if camera_info is None:
            self.get_logger().warn('Could not get camera info - cannot project gimbal fov.')
            return
        assert hasattr(camera_info, 'k'), 'Camera intrinsics matrix K not available - cannot project gimbal FoV.'
        h, w = self._img_dimensions()
        # TODO: assert h w not none and integers? and divisible by 2?

        # Intrinsic matrix
        k = np.array(camera_info.k).reshape([3, 3])
        assert k.shape == (3, 3), 'Intrinsic matrix had unexpected shape: ' + str(k.shape) \
                                  + ' - could not project gimbal FoV.'

        # Project image corners to z=0 plane (ground)
        src_corners = create_src_corners(h, w)

        e = np.delete(e, 2, 1)  # Remove z-column, making the matrix square
        p = np.matmul(k, e)
        p_inv = np.linalg.inv(p)

        dst_corners = cv2.perspectiveTransform(src_corners, p_inv)  # TODO: use util.get_fov here?
        dst_corners = dst_corners.squeeze()  # See get_fov usage elsewhere -where to do squeeze if at all?

        return dst_corners

    def _get_global_position_latlonalt(self):
        """Returns lat, lon in WGS84 and altitude in meters from vehicle global position."""
        global_position = self._global_position()
        if global_position is None:
            self.get_logger().warn('Could not get vehicle global position - returning None.')
            return None
        assert hasattr(global_position, 'lat') and hasattr(global_position, 'lon'),\
            'Global position message did not include lat or lon fields.'
        assert hasattr(global_position, 'alt'), 'Global position message did not include alt field.'
        lat, lon, alt = global_position.lat, global_position.lon, global_position.alt
        return LatLonAlt(lat, lon, alt)

    def _get_local_position_ref_latlonalt(self):
        """Returns reference lat, lon in WGS84 and altitude in meters from vehicle local position."""
        local_position = self._local_position()
        if local_position is None:
            self.get_logger().warn('Could not get vehicle local position - returning None as local frame reference.')
            return None

        # TODO: z may not be needed - make a separate _ref_latlon method!
        required_attrs = ['xy_global', 'z_global', 'ref_lat', 'ref_lon', 'ref_alt']
        assert all(hasattr(local_position, attr) for attr in required_attrs), \
            f'Required attributes {required_attrs} were not all found in local position message: {local_position}.'

        if local_position.xy_global is True and local_position.z_global is True:
            lat, lon, alt = local_position.ref_lat, local_position.ref_lon, local_position.ref_alt
            return LatLonAlt(lat, lon, alt)
        else:
            # TODO: z may not be needed - make a separate _ref_latlon method!
            self.get_logger().warn('No valid global reference for local frame origin - returning None.')
            return None

    def _update_map(self):
        """Gets latest map from WMS server and saves it."""
        global_position_latlonalt = self._get_global_position_latlonalt()
        if global_position_latlonalt is None:
            self.get_logger().warn('Could not get vehicle global position latlonalt. Cannot update map.')
            return None

        # Use these coordinates for fetching map from server
        map_center_latlon = LatLon(global_position_latlonalt.lat, global_position_latlonalt.lon)

        if self._use_gimbal_projection():
            camera_info = self._camera_info()
            if camera_info is not None:
                assert hasattr(camera_info, 'k'), 'CameraInfo does not have k, cannot compute gimbal FoV WGS84 ' \
                                                  'coordinates. '

                gimbal_fov_pix = self._project_gimbal_fov(global_position_latlonalt.alt)

                # Convert gimbal field of view from pixels to WGS84 coordinates
                if gimbal_fov_pix is not None:
                    azimuths = list(map(lambda x: math.degrees(math.atan2(x[0], x[1])), gimbal_fov_pix))
                    distances = list(map(lambda x: math.sqrt(x[0]**2 + x[1]**2), gimbal_fov_pix))
                    zipped = list(zip(azimuths, distances))
                    to_wgs84 = partial(move_distance, map_center_latlon)
                    self._gimbal_fov_wgs84 = np.array(list(map(to_wgs84, zipped)))
                    ### TODO: add some sort of assertion hat projected FoV is contained in size and makes sense

                    # Use projected field of view center instead of global position as map center
                    map_center_latlon = get_bbox_center(fov_to_bbox(self._gimbal_fov_wgs84))
                else:
                    self.get_logger().warn('Could not project camera FoV, getting map raster assuming nadir-facing '
                                           'camera.')
            else:
                self.get_logger().debug('Camera info not available, cannot project gimbal FoV, defaulting to global '
                                        'position.')

        self._map_bbox = get_bbox(map_center_latlon)

        # Build and send WMS request
        layer_str = self.get_parameter('layer').get_parameter_value().string_value
        srs_str = self.get_parameter('srs').get_parameter_value().string_value
        self.get_logger().debug(f'Getting map for bounding box: {self._map_bbox}, layer: {layer_str}, srs: {srs_str}.')
        try:
            self._map = self._wms.getmap(layers=[layer_str], srs=srs_str, bbox=self._map_bbox,
                                         size=self._map_size_with_padding(), format='image/png',
                                         transparent=True)
        except Exception as e:
            self.get_logger().warn('Exception from WMS server query: {}\n{}'.format(e, traceback.print_exc()))
            return

        # Decode response from WMS server
        self._map = np.frombuffer(self._map.read(), np.uint8)
        self._map = imdecode(self._map, cv2.IMREAD_UNCHANGED)
        assert self._map.shape[0:2] == self._map_size_with_padding(), 'Decoded map is not the specified size.'

    def _image_raw_callback(self, msg):
        """Handles reception of latest image frame from camera."""
        self.get_logger().debug('Camera image callback triggered.')
        self._topics_msgs['image_raw'] = msg

        # Get image data
        assert hasattr(msg, 'data'), f'No data present in received image message.'
        if msg.data is None:  # TODO: do an explicit type check here?
            self.get_logger().warn('No data present in received image message - cannot process image.')
            return

        cv_image = self._cv_bridge.imgmsg_to_cv2(msg, self.IMAGE_ENCODING)

        img_size = self._declared_img_size()
        if img_size is not None:
            cv_img_shape = cv_image.shape[0:2]
            declared_shape = self._declared_img_size()
            assert cv_img_shape == declared_shape, f'Converted cv_image shape {cv_img_shape} did not match declared ' \
                                                   f'image shape {declared_shape}.'

        # Get image frame_id and stamp from message header
        assert hasattr(msg, 'header'), f'No header present in received image message.'
        if msg.header is None:  # TODO: do an explicit type check here?
            self.get_logger().warn('No header present in received image message - cannot process image.')
            return
        assert hasattr(msg.header, 'frame_id'), f'No frame_id present in received image header.'
        assert hasattr(msg.header, 'stamp'), f'No stamp present in received image header.'
        frame_id = msg.header.frame_id
        timestamp = msg.header.stamp
        if frame_id is None or timestamp is None:  # TODO: do an explicit type check here?
            self.get_logger().warn(f'No frame_id or stamp in received header: {msg.header}, cannot process image.')
            return
        image_frame = ImageFrameStamp(cv_image, frame_id, timestamp)  # Use nano-seconds only from stamp

        self._match(image_frame)

    def _camera_yaw(self):
        """Returns camera yaw in degrees."""
        rpy = self._get_camera_rpy()

        assert rpy is not None, 'RPY is None, cannot retrieve camera yaw.'
        assert len(rpy) == 3, f'Unexpected length for RPY: {len(rpy)}.'
        assert hasattr(rpy, 'yaw'), f'No yaw attribute found for named tuple: {rpy}.'

        camera_yaw = rpy.yaw
        return camera_yaw

    def _get_camera_rpy(self):
        """Returns roll-pitch-yaw euler vector."""
        gimbal_attitude = self._gimbal_attitude()
        if gimbal_attitude is None:
            self.get_logger().warn('Gimbal attitude not available, cannot return RPY.')
            return None
        assert hasattr(gimbal_attitude, 'q'), 'Gimbal attitude quaternion not available - cannot compute RPY.'
        gimbal_euler = Rotation.from_quat(gimbal_attitude.q).as_euler(self.EULER_SEQUENCE, degrees=True)

        local_position = self._topics_msgs.get('VehicleLocalPosition', None)
        if local_position is None:
            self.get_logger().warn('VehicleLocalPosition is unknown, cannot get heading. Cannot return RPY.')
            return None
        assert hasattr(local_position, 'heading'), 'Heading information missing from VehicleLocalPosition message. ' \
                                                   'Cannot compute RPY. '

        pitch_index = self._pitch_index()
        assert pitch_index != -1, 'Could not identify pitch index in gimbal attitude, cannot return RPY.'

        yaw_index = self._yaw_index()
        assert yaw_index != -1, 'Could not identify yaw index in gimbal attitude, cannot return RPY.'

        self.get_logger().warn('Assuming stabilized gimbal - ignoring vehicle intrinsic pitch and roll for camera RPY.')
        self.get_logger().warn('Assuming zero roll for camera RPY.')  # TODO remove zero roll assumption

        heading = local_position.heading
        assert -math.pi <= heading <= math.pi, 'Unexpected heading value: ' + str(
            heading) + '([-pi, pi] expected). Cannot compute RPY.'
        heading = math.degrees(heading)

        gimbal_yaw = gimbal_euler[yaw_index]
        assert -180 <= gimbal_yaw <= 180, 'Unexpected gimbal yaw value: ' + str(
            heading) + '([-180, 180] expected). Cannot compute RPY.'
        yaw = heading + gimbal_yaw  # TODO: if over 180, make it negative instead
        assert abs(yaw) <= 360, f'Yaw was unexpectedly large: {abs(yaw)}, max 360 expected.'
        if abs(yaw) > 180:  # Important: >, not >= (because we are using mod 180 operation below)
            yaw = yaw % 180 if yaw < 0 else yaw % -180  # Make the compound yaw between -180 and 180 degrees
        pitch = -(90 + gimbal_euler[pitch_index])  # TODO: ensure abs(pitch) <= 90?
        roll = 0  # TODO remove zero roll assumption
        rpy = RPY(roll, pitch, yaw)

        return rpy

    def _store_previous_image_frame_stamp(self, image_frame):
        self._previous_image_frame_stamp = image_frame

    def _get_camera_normal(self):
        nadir = np.array([0, 0, 1])
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Could not get RPY - cannot compute camera normal.')
            return None

        r = Rotation.from_euler(self.EULER_SEQUENCE, list(rpy), degrees=True)
        camera_normal = r.apply(nadir)

        assert camera_normal.shape == nadir.shape, f'Unexpected camera normal shape {camera_normal.shape}.'
        # TODO: this assertion is arbitrary? how to handle unexpected camera normal length?
        camera_normal_length = np.linalg.norm(camera_normal)
        assert abs(camera_normal_length-1) <= 0.001, f'Unexpected camera normal length {camera_normal_length}.'

        return camera_normal

    def _pitch_index(self):
        return self.EULER_SEQUENCE.lower().find('y')

    def _yaw_index(self):
        return self.EULER_SEQUENCE.lower().find('x')

    def _base_callback(self, msg_name, msg):
        """Stores message and prints out brief debug log message."""
        self.get_logger().debug(msg_name + ' callback triggered.')
        self._topics_msgs[msg_name] = msg

    def _camera_info_callback(self, msg):
        """Handles reception of camera info."""
        self._base_callback('camera_info', msg)
        self.get_logger().debug('Camera info: ' + str(msg))

        # Check that key fields are present in received msg, then destroy subscription which is no longer needed
        required_attrs = ['k', 'width', 'height']
        if msg is not None and all(hasattr(msg, attr) for attr in required_attrs):
            # TODO: assume camera_info is dynamic, do not destroy subscription?
            # TODO: check that frame_id is always the same (give frame_id as configuration param?)
            self.get_logger().warn('Assuming camera_info is static - destroying the topic.')
            self._topics['camera_info'].destroy()
        else:
            self.get_logger().warn(f'Did not yet receive all required attributes {required_attrs} in camera info '
                                   f'message. Will not destroy subscription yet.')

    def _vehiclelocalposition_pubsubtopic_callback(self, msg):
        """Handles reception of latest local position estimate."""
        self._base_callback('VehicleLocalPosition', msg)

    def _vehicleglobalposition_pubsubtopic_callback(self, msg):
        """Handles reception of latest global position estimate."""
        self._base_callback('VehicleGlobalPosition', msg)
        self._update_map()

    def _gimbaldeviceattitudestatus_pubsubtopic_callback(self, msg):
        """Handles reception of GimbalDeviceAttitudeStatus messages."""
        self._base_callback('GimbalDeviceAttitudeStatus', msg)

    def _gimbaldevicesetattitude_pubsubtopic_callback(self, msg):
        """Handles reception of GimbalDeviceSetAttitude messages."""
        self._base_callback('GimbalDeviceSetAttitude', msg)

    def _vehicleattitude_pubsubtopic_callback(self, msg):
        """Handles reception of VehicleAttitude messages."""
        self._base_callback('VehicleAttitude', msg)

    def _publish_vehicle_visual_odometry(self, position, velocity):
        """Publishes a VehicleVisualOdometry message over the microRTPS bridge as defined in
        https://github.com/PX4/px4_msgs/blob/master/msg/VehicleVisualOdometry.msg. """
        module_name = 'px4_msgs.msg'   #TODO: get ffrom config file
        class_name = 'VehicleVisualOdometry'  # TODO: get from config file or look at _import_class stuff in this file
        VehicleVisualOdometry = getattr(sys.modules[module_name], class_name, None)
        assert VehicleVisualOdometry is not None, f'{class_name} was not found in module {module_name}.'
        msg = VehicleVisualOdometry()

        # TODO: could throw a warning if position and velocity BOTH are None - would publish a message full of NaN

        # Timestamp
        now = int(time.time() * 1e6)  # uint64 time in microseconds  # TODO: should be time since system start?
        msg.timestamp = now
        msg.timestamp_sample = now  # uint64 TODO: what's this?

        # Position and linear velocity local frame of reference
        msg.local_frame = self.LOCAL_FRAME_NED  # uint8

        # Position
        if position is not None:
            assert len(position) == 3, f'Unexpected length for position estimate: {len(position)} (3 expected).'  # TODO: can also be length 2 if altitude is not published, handle that
            assert all(isinstance(x, float) for x in position), f'Position contained non-float elements.'
            # TODO: check for np.float32?
            msg.x, msg.y, msg.z = position  # float32 North, East, Down
        else:
            self.get_logger().warn('Position tuple was None - publishing NaN as position.')
            msg.x, msg.y, msg.z = (float('nan'), ) * 3  # float32 North, East, Down

        # Attitude quaternions - not used
        msg.q = (float('nan'), ) * 4  # float32
        msg.q_offset = (float('nan'), ) * 4
        msg.pose_covariance = (float('nan'), ) * 21

        # Velocity frame of reference
        msg.velocity_frame = self.LOCAL_FRAME_NED  # uint8

        # Velocity
        if velocity is not None:
            assert len(velocity) == 3, f'Unexpected length for velocity estimate: {len(velocity)} (3 expected).'
            assert all(isinstance(x, float) for x in velocity), f'Velocity contained non-float elements.'
            # TODO: check for np.float32?
            msg.vx, msg.vy, msg.vz = velocity  # float32 North, East, Down
        else:
            self.get_logger().warn('Velocity tuple was None - publishing NaN as velocity.')
            msg.vx, msg.vy, msg.vz = (float('nan'), ) * 3  # float32 North, East, Down

        # Angular velocity - not used
        msg.rollspeed, msg.pitchspeed, msg.yawspeed = (float('nan'), ) * 3  # float32 TODO: remove redundant np.float32?
        msg.velocity_covariance = (float('nan'), ) * 21  # float32 North, East, Down

    def _camera_pitch(self):
        """Returns camera pitch in degrees relative to vehicle frame."""
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Gimbal RPY not available, cannot compute camera pitch.')

        assert len(rpy) == 3, 'Unexpected length of euler angles vector: ' + str(len(rpy))
        assert hasattr(rpy, 'pitch'), f'Pitch attribute not found in named tuple {rpy}.'

        return rpy.pitch

    def _gimbal_attitude(self):
        """Returns GimbalDeviceAttitudeStatus or GimbalDeviceSetAttitude if it is not available."""
        gimbal_attitude = self._topics_msgs.get('GimbalDeviceAttitudeStatus', None)
        if gimbal_attitude is None:
            # Try alternative topic
            self.get_logger().warn('GimbalDeviceAttitudeStatus not available. Trying GimbalDeviceSetAttitude instead.')
            gimbal_attitude = self._topics_msgs.get('GimbalDeviceSetAttitude', None)
            if gimbal_attitude is None:
                self.get_logger().warn('GimbalDeviceSetAttitude not available. Gimbal attitude status not available.')
        return gimbal_attitude

    def _process_matches(self, mkp_img, mkp_map, k, camera_normal, reproj_threshold=1.0, method=cv2.RANSAC, affine=False):
        """Processes matching keypoints from img and map and returns essential, and homography matrices & pose.

        Arguments:
            mkp_img - The matching keypoints from image.
            mkp_map - The matching keypoints from map.
            k - The intrinsic camera matrix.
            camera_normal - The camera normal unit vector.
            reproj_threshold - The RANSAC reprojection threshold for homography estimation.
            method - Method to use for estimation.
            affine - Boolean flag indicating that transformation should be restricted to 2D affine transformation
        """
        min_points = 4
        assert len(mkp_img) >= min_points and len(mkp_map) >= min_points, 'Four points needed to estimate homography.'
        if not affine:
            h, h_mask = cv2.findHomography(mkp_img, mkp_map, method, reproj_threshold)
        else:
            h, h_mask = cv2.estimateAffinePartial2D(mkp_img, mkp_map)
            h = np.vstack((h, np.array([0, 0, 1])))  # Make it into a homography matrix

        num, Rs, Ts, Ns = cv2.decomposeHomographyMat(h, k)

        # Get the one where angle between plane normal and inverse of camera normal is smallest
        # Plane is defined by Z=0 and "up" is in the negative direction on the z-axis in this case
        get_angle_partial = partial(get_angle, -camera_normal)
        angles = list(map(get_angle_partial, Ns))
        index_of_smallest_angle = angles.index(min(angles))
        rotation, translation = Rs[index_of_smallest_angle], Ts[index_of_smallest_angle]

        self.get_logger().debug('decomposition R:\n{}.'.format(rotation))
        self.get_logger().debug('decomposition T:\n{}.'.format(translation))
        self.get_logger().debug('decomposition Ns:\n{}.'.format(Ns))
        self.get_logger().debug('decomposition Ns angles:\n{}.'.format(angles))
        self.get_logger().debug('decomposition smallest angle index:\n{}.'.format(index_of_smallest_angle))
        self.get_logger().debug('decomposition smallest angle:\n{}.'.format(min(angles)))

        return h, h_mask, translation, rotation

    def _match(self, image_frame):
        """Matches camera image to map image and computes camera position and field of view."""
        try:
            self.get_logger().debug('Matching image to map.')

            if self._map is None:
                self.get_logger().warn('Map not yet available - skipping matching.')
                return

            yaw = self._camera_yaw()
            if yaw is None:
                self.get_logger().warn('Could not get camera yaw. Skipping matching.')
                return
            rot = math.radians(yaw)
            assert -math.pi <= rot <= math.pi, 'Unexpected gimbal yaw value: ' + str(rot) + ' ([-pi, pi] expected).'
            self.get_logger().debug('Current camera yaw: ' + str(rot) + ' radians.')

            map_cropped = rotate_and_crop_map(self._map, rot, self._img_dimensions())
            assert map_cropped.shape[0:2] == self._declared_img_size(), 'Cropped map did not match declared shape.'

            mkp_img, mkp_map = self._superglue.match(image_frame.image, map_cropped)

            match_count_img = len(mkp_img)
            assert match_count_img == len(mkp_map), 'Matched keypoint counts did not match.'
            if match_count_img < self.MINIMUM_MATCHES:
                self.get_logger().warn(f'Found {match_count_img} matches, {self.MINIMUM_MATCHES} required. Skip frame.')
                return

            camera_normal = self._get_camera_normal()
            if camera_normal is None:
                self.get_logger().warn('Could not get camera normal. Skipping matching.')
                return

            camera_info = self._camera_info()
            if camera_info is None:
                self.get_logger().warn('Could not get camera info. Skipping matching.')
                return
            assert hasattr(camera_info, 'k'), 'Camera info did not have k - cannot match.'
            assert len(camera_info.k) == 9, 'K had unexpected length.'
            k = camera_info.k.reshape([3, 3])

            h, h_mask, t, r = self._process_matches(mkp_img, mkp_map, k, camera_normal, affine=self._restrict_affine())

            assert h.shape == (3, 3), f'Homography matrix had unexpected shape: {h.shape}.'
            assert t.shape == (3, 1), f'Translation vector had unexpected shape: {t.shape}.'
            assert r.shape == (3, 3), f'Rotation matrix had unexpected shape: {r.shape}.'

            fov_pix = get_fov(image_frame.image, h)
            visualize_homography('Matches and FoV', image_frame.image, map_cropped, mkp_img, mkp_map, fov_pix)

            map_lat, map_lon = get_bbox_center(BBox(*self._map_bbox))

            map_dims_with_padding = self._map_dimensions_with_padding()
            if map_dims_with_padding is None:
                self.get_logger().warn('Could not get map dimensions info. Skipping matching.')
                return

            img_dimensions = self._img_dimensions()
            if map_dims_with_padding is None:
                self.get_logger().warn('Could not get img dimensions info. Skipping matching.')
                return

            fov_wgs84, fov_uncropped, fov_unrotated = convert_fov_from_pix_to_wgs84(
                fov_pix, map_dims_with_padding, self._map_bbox, rot, img_dimensions)
            fov_set = image_frame.set_estimated_fov(fov_wgs84)  # Store field of view in frame
            assert fov_set is True, f'Something went wrong - field of view was already set earlier.'

            # Compute camera altitude, and distance to principal point using triangle similarity
            # TODO: _update_map has similar logic used in gimbal fov projection, try to combine
            fov_center_line_length = get_distance_of_fov_center(fov_wgs84)
            focal_length = k[0][0]
            assert hasattr(img_dimensions, 'width') and hasattr(img_dimensions, 'height'), \
                'Img dimensions did not have expected attributes.'
            camera_distance = get_camera_distance(focal_length, img_dimensions.width, fov_center_line_length)
            camera_pitch = self._camera_pitch()
            camera_altitude = None
            if camera_pitch is None:
                # TODO: Use some other method to estimate altitude if pitch not available?
                self.get_logger().warn('Camera pitch not available - cannot estimate altitude visually.')
            else:
                camera_altitude = math.cos(math.radians(camera_pitch)) * camera_distance  # TODO: use rotation from decomposeHomography for getting the pitch in this case (use visual info, not from sensors)
            self.get_logger().debug(f'Camera pitch {camera_pitch} deg, distance to principal point {camera_distance} m,'
                                    f' altitude {camera_altitude} m.')

            """
            mkp_map_uncropped = []
            for i in range(0, len(mkp_map)):
                mkp_map_uncropped.append(list(
                    uncrop_pixel_coordinates(self._img_dimensions(), self._map_dimensions_with_padding(), mkp_map[i])))
            mkp_map_uncropped = np.array(mkp_map_uncropped)

            mkp_map_unrotated = []
            for i in range(0, len(mkp_map_uncropped)):
                mkp_map_unrotated.append(
                    list(rotate_point(rot, self._map_dimensions_with_padding(), mkp_map_uncropped[i])))
            mkp_map_unrotated = np.array(mkp_map_unrotated)

            h2, h_mask2, translation_vector2, rotation_matrix2 = self._process_matches(mkp_img, mkp_map_unrotated,
                                                                                 # mkp_map_uncropped,
                                                                                 self._camera_info().k.reshape([3, 3]),
                                                                                 cam_normal,
                                                                                 affine=
                                                                                 self._config['misc']['affine'])

            fov_pix_2 = get_fov(self._cv_image, h2)
            visualize_homography('Uncropped and unrotated', self._cv_image, self._map, mkp_img, mkp_map_unrotated,
                                 fov_pix_2)  # TODO: separate calculation of fov_pix from their visualization!
            """

            # Convert translation vector to WGS84 coordinates
            # Translate relative to top left corner, not principal point/center of map raster
            h, w = img_dimensions
            t[0] = (1 - t[0]) * w / 2
            t[1] = (1 - t[1]) * h / 2
            cam_pos_wgs84, cam_pos_wgs84_uncropped, cam_pos_wgs84_unrotated = convert_fov_from_pix_to_wgs84(  # TODO: break this func into an array and single version?
                np.array(t[0:2].reshape((1, 1, 2))), map_dims_with_padding,
                self._map_bbox, rot, img_dimensions)
            cam_pos_wgs84 = cam_pos_wgs84.squeeze()  # TODO: eliminate need for this squeeze
            # TODO: turn cam_pos_wgs84 into a LatLonAlt
            # TODO: something is wrong with camera_altitude - should be a scalar but is array
            lalt = LatLonAlt(*(tuple(cam_pos_wgs84) + (camera_altitude,)))  # TODO: alt should not be None? Use LatLon instead?
            cam_pos_set = image_frame.set_estimated_camera_position(lalt)  # Store the camera position in the frame (should not return False)
            assert cam_pos_set is True, f'Something went wrong - camera position was already set earlier.'


            fov_gimbal = self._gimbal_fov_wgs84
            write_fov_and_camera_location_to_geojson(fov_wgs84, cam_pos_wgs84, (map_lat, map_lon, camera_distance),
                                                     fov_gimbal)

            # Compute position (meters) and velocity (meters/second) in local frame
            local_position = None
            local_frame_origin_latlonalt = self._get_local_position_ref_latlonalt()
            if local_frame_origin_latlonalt is not None:
                local_position = distances(local_frame_origin_latlonalt, LatLon(*tuple(cam_pos_wgs84))) \
                                 + (camera_altitude,)  # TODO: see lalt and set_esitmated_camera_position call above - should not need to do this twice?
            else:
                self.get_logger().debug(f'Could not get local frame origin - will not compute local position.')

            velocity = None
            # TODO: Make it so that previous global position can be fetched without risk of mixing the order of these operations (e.g. use timestamps and/or frame_id or something).
            if self._previous_image_frame_stamp is not None:
                previous_camera_global_position = self._previous_image_frame_stamp.get_estimated_camera_position()
                assert previous_camera_global_position is not None, f'Previous camera position was unexpectedly None.'  # TODO: is it possible that this is None? Need to do warning instead of assert?
                current_camera_global_position = image_frame.get_estimated_camera_position() # TODO: # Could use cam_pos_wgs84 directly but its somewhere up there - should break _match down into smaller units
                assert current_camera_global_position is not None, f'Current camera position was unexpectedly None.'  # TODO: is it possible that this is None? Need to do warning instead of assert?
                assert hasattr(self._previous_image_frame_stamp, 'timestamp'),\
                    'Previous image frame timstamp not found.'

                # TODO: refactor this assertion so that it's more compact
                if self._previous_image_frame_stamp.timestamp.sec == image_frame.timestamp.sec:
                    assert self._previous_image_frame_stamp.timestamp.nanonsec < image_frame.timestamp.nanosec, \
                        f'Previous image frame timestamp {self._previous_image_frame_stamp.timestamp} was >= than ' \
                        f'current image frame timestamp {image_frame.timestamp}.'
                else:
                    assert self._previous_image_frame_stamp.timestamp.sec < image_frame.timestamp.sec,\
                        f'Previous image frame timestamp {self._previous_image_frame_stamp.timestamp} was >= than ' \
                        f'current image frame timestamp {image_frame.timestamp}.'
                time_difference = image_frame.timestamp.sec - self._previous_image_frame_stamp.timestamp.sec
                if time_difference == 0:
                    time_difference = (image_frame.timestamp.nanosec -
                                       self._previous_image_frame_stamp.timestamp.nanosec) / 1e9
                assert time_difference > 0, f'Time difference between frames was 0.'
                x_dist, y_dist = distances(current_camera_global_position, previous_camera_global_position)  # TODO: compute x,y,z components separately!
                z_dist = current_camera_global_position.alt - previous_camera_global_position.alt
                dist = (x_dist, y_dist, z_dist)
                assert all(isinstance(x, float) for x in dist), f'Expected all float values for distance: {dist}.'  # TODO: z could be None/NaN - handle it!
                velocity = tuple(x / time_difference for x in dist)
            else:
                self.get_logger().warning(f'Could not get previous image frame stamp - will not compute velocity.')

            self.get_logger().debug(f'Local frame position: {local_position}, velocity: {velocity}.')
            self.get_logger().debug(f'Local frame origin: {self._get_local_position_ref_latlonalt()}.')
            self._publish_vehicle_visual_odometry(local_position, velocity)  # TODO: enable

            self._store_previous_image_frame_stamp(image_frame)  # Store previous position along with previous frame_id and timestamp

        except Exception as e:
            self.get_logger().error('Matching returned exception: {}\n{}'.format(e, traceback.print_exc()))


def main(args=None):
    rclpy.init(args=args)
    matcher = Matcher(share_dir, superglue_dir)
    rclpy.spin(matcher)
    matcher.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
