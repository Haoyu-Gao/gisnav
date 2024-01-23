"""This module contains :class:`.BBoxNode`, a :term:`ROS` node for computing
and publishing a :term:`bounding box` of the :term:`camera's <camera>`
ground-projected :term:`field of view <FOV>`.
"""
from typing import Final, Optional

import numpy as np
import pyproj
import tf_transformations
from geographic_msgs.msg import BoundingBox, GeoPoint, GeoPose, GeoPoseStamped
from geometry_msgs.msg import PoseStamped, Quaternion
from mavros_msgs.msg import GimbalDeviceAttitudeStatus
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import CameraInfo, NavSatFix

from .. import messaging
from .._decorators import ROS, narrow_types
from ..static_configuration import (
    ROS_TOPIC_RELATIVE_CAMERA_GEOPOSE,
    ROS_TOPIC_RELATIVE_FOV_BOUNDING_BOX,
)


class BBoxNode(Node):
    """Publishes :class:`.BoundingBox` of the :term:`camera's <camera>`
    ground-projected :term:`field of view <FOV>`.
    """

    _ROS_PARAM_DESCRIPTOR_READ_ONLY: Final = ParameterDescriptor(read_only=True)
    """A read only ROS parameter descriptor"""

    def __init__(self, *args, **kwargs):
        """Class initializer

        :param args: Positional arguments to parent :class:`.Node` constructor
        :param kwargs: Keyword arguments to parent :class:`.Node` constructor
        """
        super().__init__(*args, **kwargs)

        # Calling these decorated properties the first time will setup
        # subscriptions to the appropriate ROS topics
        self.camera_info
        self.nav_sat_fix
        self.vehicle_pose
        self.gimbal_device_attitude_status

    def _nav_sat_fix_cb(self, msg: NavSatFix) -> None:
        """Callback for the :term:`global position` message from the
        :term:`navigation filter`
        """
        self.fov_bounding_box
        self.camera_geopose
        # self.vehicle_geopose  # triggered by camera geopose

    @property
    @ROS.max_delay_ms(messaging.DELAY_DEFAULT_MS)
    @ROS.subscribe(
        "/mavros/global_position/global",
        QoSPresetProfiles.SENSOR_DATA.value,
        callback=_nav_sat_fix_cb,
    )
    def nav_sat_fix(self) -> Optional[NavSatFix]:
        """Vehicle GPS fix, or None if unknown or too old"""

    @property
    # @ROS.max_delay_ms(messaging.DELAY_FAST_MS)  # TODO:
    @ROS.subscribe(
        "/mavros/local_position/pose",
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def vehicle_pose(self) -> Optional[PoseStamped]:
        """Vehicle local position, or None if not available or too old"""

    @property
    # @ROS.max_delay_ms(messaging.DELAY_DEFAULT_MS) - camera info has no header (?)
    @ROS.subscribe(messaging.ROS_TOPIC_CAMERA_INFO, QoSPresetProfiles.SENSOR_DATA.value)
    def camera_info(self) -> Optional[CameraInfo]:
        """Camera info for determining appropriate :attr:`.orthoimage` resolution"""

    @property
    @ROS.publish(
        ROS_TOPIC_RELATIVE_FOV_BOUNDING_BOX, QoSPresetProfiles.SENSOR_DATA.value
    )
    def fov_bounding_box(self) -> Optional[BoundingBox]:
        """:class:`.BoundingBox` of the :term:`camera's <camera>` ground-projected
        :term:`field of view <FOV>`.
        """

        @narrow_types(self)
        def _fov_and_principal_point_on_ground_plane(
            camera_quaternion: Quaternion,
            vehicle_pose: PoseStamped,
            camera_info: CameraInfo,
        ) -> Optional[np.ndarray]:
            """Projects :term:`camera` principal point and :term:`FOV` corners
             on ground plane

            .. note::
                Assumes ground is a flat plane, does not take :term:`DEM` into account

            :return: Numpy array of FOV corners and principal point projected onto
                ground (vehicle :term:`local position` z==0) plane in following
                order: top-left, top-right, bottom-right, bottom-left, principal point.
                Shape is (5, 2). Coordinates are meters in local tangent plane
                :term:`ENU`.
            """
            R = tf_transformations.quaternion_matrix(
                tuple(messaging.as_np_quaternion(camera_quaternion))
            )[:3, :3]

            # Camera position in LTP centered in current location (not EKF local
            # frame origin - only shares the z-coordinate!) - assume local
            # frame z is altitude AGL
            position = vehicle_pose.pose.position
            C = np.array((0, 0, position.z))

            intrinsics = camera_info.k.reshape((3, 3))

            # List of image points: top-left, top-right, bottom-right, bottom-left,
            # principal point
            img_points = [
                [0, 0],
                [camera_info.width - 1, 0],
                [camera_info.width - 1, camera_info.height - 1],
                [0, camera_info.height - 1],
                [camera_info.width / 2, camera_info.height / 2],
            ]

            # Project each point to the ground
            ground_points = []
            for pt in img_points:
                u, v = pt

                # Convert to normalized image coordinates
                d_img = np.array([u, v, 1])

                try:
                    d_cam = np.linalg.inv(intrinsics) @ d_img
                except np.linalg.LinAlgError as _:  # noqa: F841
                    self.get_logger().error(
                        "Could not invert camera intrinsics matrix. Cannot"
                        "project FOV on ground."
                    )
                    return None

                # Convert direction to ENU frame
                d_enu = R @ d_cam

                # Find intersection with ground plane
                t = -C[2] / d_enu[2]
                intersection = C + t * d_enu

                ground_points.append(intersection[:2])

            return np.vstack(ground_points)

        @narrow_types(self)
        def _enu_to_latlon(
            bbox_coords: np.ndarray, navsatfix: NavSatFix
        ) -> Optional[np.ndarray]:
            """Convert :term:`ENU` local tangent plane coordinates to
            latitude and longitude.

            :param bbox_coords: A bounding box in local ENU frame (units in meters)
            :param navsatfix: :term:`Vehicle` :term:`global position`

            :return: Same bounding box in WGS 84 coordinates
            """

            def _determine_utm_zone(longitude):
                """Determine the UTM zone for a given longitude."""
                return int((longitude + 180) / 6) + 1

            # Define the UTM zone and conversion
            proj_latlon = pyproj.Proj(proj="latlong", datum="WGS84")
            utm_zone = _determine_utm_zone(navsatfix.longitude)
            proj_utm = pyproj.Proj(proj="utm", zone=utm_zone, datum="WGS84")

            # Convert origin to UTM
            origin_x, origin_y = pyproj.transform(
                proj_latlon, proj_utm, navsatfix.longitude, navsatfix.latitude
            )

            # Add ENU offsets to the UTM origin
            utm_x = origin_x + bbox_coords[:, 0]
            utm_y = origin_y + bbox_coords[:, 1]

            # Convert back to lat/lon
            lon, lat = pyproj.transform(proj_utm, proj_latlon, utm_x, utm_y)

            latlon_coords = np.column_stack((lon, lat))
            assert latlon_coords.shape == bbox_coords.shape

            return latlon_coords

        @narrow_types(self)
        def _square_bounding_box(enu_coords: np.ndarray) -> np.ndarray:
            """
            Adjust the given bounding box to ensure it's square in the ENU local
            tangent plane (meters).

            Adds padding in X (easting) and Y (northing) directions to ensure
            camera FOV is fully enclosed by the bounding box, and to reduce need
            to update the reference image so often.

            :param enu_coords: A numpy array of shape (N, 2) representing ENU
                coordinates.
            :return: A numpy array of shape (N, 2) representing the adjusted
                square bounding box.
            """
            min_e, min_n = np.min(enu_coords, axis=0)
            max_e, max_n = np.max(enu_coords, axis=0)

            delta_e = max_e - min_e
            delta_n = max_n - min_n

            if delta_e > delta_n:
                # Expand in the north direction
                difference = (delta_e - delta_n) / 2
                min_n -= difference
                max_n += difference
            elif delta_n > delta_e:
                # Expand in the east direction
                difference = (delta_n - delta_e) / 2
                min_e -= difference
                max_e += difference

            # Construct the squared bounding box coordinates
            # Add padding to bounding box by expanding field of view bounding
            # box width in each direction
            padding = max_n - min_n
            square_box = np.array(
                [
                    [min_e - padding, min_n - padding],
                    [max_e + padding, min_n - padding],
                    [max_e + padding, max_n + padding],
                    [min_e - padding, max_n + padding],
                ]
            )

            assert square_box.shape == enu_coords.shape

            return square_box

        @narrow_types(self)
        def _bounding_box(
            fov_local_enu: np.ndarray,
        ) -> BoundingBox:
            """Create a BoundingBox :term:`message` that envelops the provided
            :term:`FOV` coordinates.

            fov_local_enu: A 4x2 numpy array where N is the number of points,
                        and each row represents [longitude, latitude].

            Returns: geographic_msgs.msg.BoundingBox
            """
            assert fov_local_enu.shape == (4, 2)

            # Find the min and max values for longitude and latitude
            min_lon, min_lat = np.min(fov_local_enu, axis=0)
            max_lon, max_lat = np.max(fov_local_enu, axis=0)

            # Create and populate the BoundingBox message
            bbox = BoundingBox()
            bbox.min_pt.latitude = min_lat
            bbox.min_pt.longitude = min_lon
            bbox.max_pt.latitude = max_lat
            bbox.max_pt.longitude = max_lon

            return bbox

        fov_and_c_on_ground_local_enu = _fov_and_principal_point_on_ground_plane(
            self._camera_quaternion, self.vehicle_pose, self.camera_info
        )
        if fov_and_c_on_ground_local_enu is not None:
            fov_on_ground_local_enu = fov_and_c_on_ground_local_enu[:4]
            bbox_local_enu_padded_square = _square_bounding_box(fov_on_ground_local_enu)
            bounding_box = _enu_to_latlon(
                bbox_local_enu_padded_square, self.nav_sat_fix
            )
            # Convert from numpy array to BoundingBox
            bounding_box = _bounding_box(bounding_box)
        else:
            bounding_box = None

        # TODO: here there used to be a fallback that would get bbox u
        #  nder
        #  vehicle if FOV could not be projected. But that should not be needed
        #  if everything works so it was removed from here.

        return bounding_box

    def _gimbal_device_attitude_status_cb(
        self, msg: GimbalDeviceAttitudeStatus
    ) -> None:
        """Callback for :class:`mavros_msgs.msg.GimbalDeviceAttitudeStatus` message

        :param msg: :class:`mavros_msgs.msg.GimbalDeviceAttitudeStatus` message
            from MAVROS
        """
        self.fov_bounding_box
        self.camera_geopose
        # self.vehicle_geopose  # triggered by camera geopose

    @property
    # @ROS.max_delay_ms(messaging.DELAY_FAST_MS)  # TODO re-enable
    @ROS.subscribe(
        "/mavros/gimbal_control/device/attitude_status",
        QoSPresetProfiles.SENSOR_DATA.value,
        callback=_gimbal_device_attitude_status_cb,
    )
    def gimbal_device_attitude_status(self) -> Optional[GimbalDeviceAttitudeStatus]:
        """:term:`Camera` :term:`FRD` :term:`orientation`, or None if not available
        or too old
        """

    @property
    def _camera_quaternion(self) -> Optional[Quaternion]:
        """:term:`Camera` :term:`ENU` :term:`orientation` or None if not available

        .. note::
            * Current implementation assumes camera faces directly down from
              :term:`vehicle` body if GimbalDeviceAttitudeStatus :term:`message`
              (:term:`MAVLink` gimbal protocol v2) is not available. Should
              probably not be used for estimating :term:`vehicle` :term:`orientation`.
            * If GimbalDeviceAttitudeStatus :term:`message`
              (:term:`MAVLink` gimbal protocol v2) is available, only the flags
              value of 12 i.e. bit mask 1100 (horizon-locked pitch and roll,
              floating yaw) is supported.
        """

        def _normalize_quaternion(q: Quaternion) -> Quaternion:
            norm = np.sqrt(q.w**2 + q.x**2 + q.y**2 + q.z**2)
            q.w = q.w / norm
            q.x = q.x / norm
            q.y = q.y / norm
            q.z = q.z / norm
            return q

        @narrow_types(self)
        def _camera_quaternion(
            geopose: GeoPoseStamped,
            gimbal_device_attitude_status: Optional[GimbalDeviceAttitudeStatus],
        ):
            """:term:`Camera` :term:`orientation` quaternion in :term:`ENU` frame"""
            if gimbal_device_attitude_status is None:
                # Identity quaternion: assume down facing camera if no
                # information received from autopilot bridge
                camera_enu_q = Quaternion(
                    x=0.0,
                    y=0.0,
                    z=0.0,
                    w=1.0,
                )
            else:
                assert gimbal_device_attitude_status.flags == 12, (
                    "Currently GISNav only supports a two-axis gimbal that has "
                    "horizon-locked roll and pitch (MAVLink Gimbal Protocol v2 "
                    "GimbalDeviceAttitudeStatus message flags has value 12 i.e. "
                    "1100 for bit mask)."
                )
                # TODO: handle failure flags (e.g. gimbal at physical limit)
                # TODO: handle gimbal lock flags (especially yaw lock, flags == 16)

                # Extract yaw-only quaternion from vehicle's quaternion
                # because the gimbal quaternion has floating yaw
                vehicle_q = geopose.pose.orientation
                vehicle_yaw_only_q = Quaternion(
                    w=vehicle_q.w, x=0.0, y=0.0, z=vehicle_q.z
                )
                vehicle_yaw_only_q = _normalize_quaternion(vehicle_yaw_only_q)

                # Need to mirror gimbal orientation along vehicle Y and Z-axis in
                # FRD frame to get the camera ENU quaternion to display
                # correctly in rviz - TODO figure out why
                gimbal_enu_mirrored_q = (
                    gimbal_device_attitude_status.q.x,
                    -gimbal_device_attitude_status.q.y,
                    -gimbal_device_attitude_status.q.z,
                    gimbal_device_attitude_status.q.w,
                )

                camera_enu_q = tf_transformations.quaternion_multiply(
                    tuple(messaging.as_np_quaternion(vehicle_yaw_only_q)),
                    gimbal_enu_mirrored_q,
                )

            assert camera_enu_q is not None
            return messaging.as_ros_quaternion(np.array(camera_enu_q))

        return _camera_quaternion(
            self.vehicle_geopose, self.gimbal_device_attitude_status
        )

    @property
    @ROS.publish(ROS_TOPIC_RELATIVE_CAMERA_GEOPOSE, QoSPresetProfiles.SENSOR_DATA.value)
    def camera_geopose(self) -> Optional[GeoPoseStamped]:
        """:term:`Camera` :term:`geopose` or None if not available"""

        @narrow_types(self)
        def _camera_geopose(
            vehicle_geopose: GeoPoseStamped, camera_quaternion: Quaternion
        ) -> GeoPoseStamped:
            camera_geopose = vehicle_geopose
            camera_geopose.pose.orientation = camera_quaternion
            return camera_geopose

        return _camera_geopose(self._vehicle_geopose, self._camera_quaternion)

    def _vehicle_geopose(self) -> Optional[GeoPoseStamped]:
        """Published :term:`vehicle` :term:`geopose`, or None if not available"""

        @narrow_types(self)
        @ROS.retain_oldest_header
        def _vehicle_geopose(nav_sat_fix: NavSatFix, pose_stamped: PoseStamped) -> GeoPoseStamped:
            # Position
            latitude, longitude = (
                nav_sat_fix.latitude,
                nav_sat_fix.longitude,
            )
            altitude = nav_sat_fix.altitude

            return GeoPoseStamped(
                pose=GeoPose(
                    position=GeoPoint(
                        latitude=latitude, longitude=longitude, altitude=altitude
                    ),
                    orientation=pose_stamped.pose.orientation,
                ),
            )

        return _vehicle_geopose(self.nav_sat_fix, self.vehicle_pose)
