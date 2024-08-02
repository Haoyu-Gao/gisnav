"""This module contains :class:`.UORBNode`, an extension ROS node that publishes PX4
uORB :class:`.SensorGps` (GNSS) messages to the uXRCE-DDS middleware
"""
import time
from typing import Final, Optional, Tuple

import numpy as np
import rclpy
import tf2_geometry_msgs
import tf2_ros
import tf_transformations
from builtin_interfaces.msg import Time
from geometry_msgs.msg import PointStamped, TwistWithCovariance, Vector3
from nav_msgs.msg import Odometry
from pyproj import Transformer
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from ublox_msgs.msg import NavPVT

from .. import _transformations as tf_
from .._decorators import ROS, narrow_types
from ..constants import (
    ROS_TOPIC_RELATIVE_NAV_PVT,
    ROS_TOPIC_ROBOT_LOCALIZATION_ODOMETRY,
)

_ROS_PARAM_DESCRIPTOR_READ_ONLY: Final = ParameterDescriptor(read_only=True)
"""A read only ROS parameter descriptor"""


class UBXNode(Node):
    """A node that publishes UBX messages to FCU via serial port"""

    ROS_D_DEM_VERTICAL_DATUM = 5703
    """Default for :attr:`.dem_vertical_datum`"""

    _REQUIRED_ODOMETRY_MESSAGES_BEFORE_PUBLISH = 10
    """Number of required odometry messages before we start publishing

    This gives some time for the internal state of the EKF to catch up with the actual
    state in case it starts from zero. Ideally we should be able to initialize both
    pose and twist and not have to wait for the filter state to catch up.
    """

    ROS_D_PORT = "/dev/ttyS1"
    """Default for :attr:`.port`"""

    ROS_D_BAUDRATE = 9600
    """Default for :attr:`.baudrate`"""

    # EPSG code for WGS 84 and a common mean sea level datum (e.g., EGM96)
    _EPSG_WGS84 = 4326
    _EPSG_MSL = 5773  # Example: EGM96

    def __init__(self, *args, **kwargs):
        """Class initializer

        :param args: Positional arguments to parent :class:`.Node` constructor
        :param kwargs: Keyword arguments to parent :class:`.Node` constructor
        """
        super().__init__(*args, **kwargs)

        self._tf_buffer = tf2_ros.Buffer(rclpy.duration.Duration(seconds=30))
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)
        self._latest_global_match_stamp: Optional[Time] = None

        # Create transformers
        self._transformer_to_wgs84 = Transformer.from_crs(
            f"EPSG:{self.dem_vertical_datum}",
            f"EPSG:{self._EPSG_WGS84}",
            always_xy=True,
        )
        self._transformer_to_msl = Transformer.from_crs(
            f"EPSG:{self.dem_vertical_datum}", f"EPSG:{self._EPSG_MSL}", always_xy=True
        )

        self._received_odometry_counter: int = 0

        # Subscribe
        self.odometry

    @property
    @ROS.parameter(ROS_D_PORT, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def port(self) -> Optional[str]:
        """Serial port for outgoing u-blox messages"""

    @property
    @ROS.parameter(ROS_D_BAUDRATE, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def baudrate(self) -> Optional[int]:
        """Baudrate for outgoing u-blox messages"""

    @property
    @ROS.parameter(ROS_D_DEM_VERTICAL_DATUM, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def dem_vertical_datum(self) -> Optional[int]:
        """DEM vertical datum

        > [!IMPORTANT]
        > Must match DEM that is published in :attr:`.GISNode.orthoimage`
        """

    def _odometry_cb(self, msg: Odometry) -> None:
        """Callback for :attr:`.odometry`"""
        if msg.header.frame_id == "gisnav_odom":
            if (
                self._received_odometry_counter
                >= self._REQUIRED_ODOMETRY_MESSAGES_BEFORE_PUBLISH
            ):
                # Only publish mock GPS messages from VO odometry
                # Using odometry derived from global EKF would greatly overestimate
                # velocity because the map to odom transform jumps around - vehicle is
                # not actually doing that.
                self._publish(msg)
            else:
                remaining = (
                    self._REQUIRED_ODOMETRY_MESSAGES_BEFORE_PUBLISH
                    - self._received_odometry_counter
                )
                self.get_logger().info(
                    f"Waiting for filter state to catch up - still need "
                    f"{remaining} more messages"
                )
                self._received_odometry_counter += 1
        else:
            assert msg.header.frame_id == "gisnav_map"
            self._latest_global_match_stamp = msg.header.stamp

    @property
    @ROS.subscribe(
        ROS_TOPIC_ROBOT_LOCALIZATION_ODOMETRY,
        QoSPresetProfiles.SENSOR_DATA.value,
        callback=_odometry_cb,
    )
    def odometry(self) -> Optional[Odometry]:
        """Subscribed filtered odometry from ``robot_localization`` package EKF node,
        or None if unknown"""

    def _publish(self, odometry: Odometry) -> None:
        @narrow_types(self)
        def _publish_inner(odometry: Odometry) -> None:
            mock_gps_dict = tf_.odom_to_typed_dict(self, odometry)
            if mock_gps_dict is not None:
                self.nav_pvt(**mock_gps_dict)
            else:
                self.get_logger().warning("Skipping publishing NavPVT")

        _publish_inner(odometry)

    @narrow_types
    @ROS.publish(ROS_TOPIC_RELATIVE_NAV_PVT, 10)  # QoSPresetProfiles.SENSOR_DATA.value,
    def nav_pvt(
        self,
        lat: int,
        lon: int,
        altitude_ellipsoid: float,
        altitude_amsl: float,
        yaw_degrees: int,
        h_variance_rad: float,
        vel_n_m_s: float,
        vel_e_m_s: float,
        vel_d_m_s: float,
        cog_rad: float,
        s_variance_m_s: float,
        timestamp: int,
        eph: float,
        epv: float,
        satellites_visible: int,
    ) -> Optional[NavPVT]:
        """Retusn UBX mock GPS message, or None if cannot be computed"""
        msg = NavPVT()

        try:
            # Convert timestamp to GPS time of week
            gps_week, time_of_week = self.unix_to_gps_time(
                timestamp / 1e6
            )  # Assuming timestamp is in microseconds

            msg.iTOW = int(time_of_week * 1000)  # GPS time of week in ms
            (
                msg.year,
                msg.month,
                msg.day,
                msg.hour,
                msg.min,
                msg.sec,
            ) = self.get_utc_time(timestamp / 1e6)

            msg.valid = (
                0x01 | 0x02 | 0x04
            )  # Assuming valid date, time, and fully resolved
            msg.tAcc = 50000000  # Time accuracy estimate in ns (50ms)
            msg.nano = 0  # Fraction of second, range -1e9 .. 1e9 (UTC)

            msg.fixType = 3  # 3D-Fix
            msg.flags = 0x01  # gnssFixOK
            msg.flags2 = 0
            msg.numSV = satellites_visible

            msg.lon = lon
            msg.lat = lat
            msg.height = int(
                altitude_ellipsoid * int(1e3)
            )  # Height above ellipsoid in mm
            msg.hMSL = int(
                altitude_amsl * int(1e3)
            )  # Height above mean sea level in mm
            msg.hAcc = int(eph * int(1e3))  # Horizontal accuracy estimate in mm
            msg.vAcc = int(epv * int(1e3))  # Vertical accuracy estimate in mm

            msg.velN = int(vel_n_m_s * int(1e3))  # NED north velocity in mm/s
            msg.velE = int(vel_e_m_s * int(1e3))  # NED east velocity in mm/s
            msg.velD = int(vel_d_m_s * int(1e3))  # NED down velocity in mm/s
            msg.gSpeed = int(
                np.sqrt(vel_n_m_s**2 + vel_e_m_s**2) * int(1e3)
            )  # Ground Speed (2-D) in mm/s
            msg.headMot = int(
                np.degrees(cog_rad) * int(1e5)
            )  # Heading of motion (2-D) in degrees * 1e-5

            msg.sAcc = int(s_variance_m_s * int(1e3))  # Speed accuracy estimate in mm/s
            msg.headAcc = int(
                np.degrees(h_variance_rad) * int(1e5)
            )  # Heading accuracy estimate in degrees * 1e-5

            msg.pDOP = 0  # Position DOP * 0.01 (unitless)

            msg.headVeh = int(
                yaw_degrees * 100000
            )  # Heading of vehicle (2-D) in degrees * 1e-5
        except AssertionError as e:
            self.get_logger().warning(
                f"Could not create mock GPS message due to exception: {e}"
            )
            msg = None

        return msg

    @narrow_types
    def _convert_to_wgs84(
        self, lat: float, lon: float, elevation: float
    ) -> Optional[Tuple[float, float]]:
        """Converts elevation or altitude from :attr:`.dem_vertical_datum` to WGS 84.

        :param lat: Latitude in decimal degrees.
        :param lon: Longitude in decimal degrees.
        :param elevation: Elevation in the specified datum.
        :return: A tuple containing elevation above WGS 84 ellipsoid and AMSL.
        """
        _, _, wgs84_elevation = self._transformer_to_wgs84.transform(
            lon, lat, elevation
        )
        _, _, msl_elevation = self._transformer_to_msl.transform(lon, lat, elevation)

        return wgs84_elevation, msl_elevation

    def _transform_twist_with_covariance(
        self, twist_with_cov, stamp, from_frame, to_frame
    ):
        # Transform the linear component
        ts = rclpy.time.Time(seconds=stamp.sec, nanoseconds=stamp.nanosec)
        point = PointStamped()
        point.header.frame_id = from_frame
        point.header.stamp = ts.to_msg()  # stamp
        point.point.x = twist_with_cov.twist.linear.x
        point.point.y = twist_with_cov.twist.linear.y
        point.point.z = twist_with_cov.twist.linear.z

        try:
            # Get the transformation matrix
            transform = tf_.lookup_transform(
                self._tf_buffer,
                to_frame,
                from_frame,
                time_duration=(stamp, rclpy.duration.Duration(seconds=0.2)),
                logger=self.get_logger(),
            )
            if transform is None:
                return None
            # Set transform linear component to zero, only use orientation since
            # we are applying this to a velocity
            transform.transform.translation = Vector3()
            transformed_point = tf2_geometry_msgs.do_transform_point(point, transform)

            quat = [
                transform.transform.rotation.x,
                transform.transform.rotation.y,
                transform.transform.rotation.z,
                transform.transform.rotation.w,
            ]
            # Get the rotation matrix from the quaternion
            rot_matrix = tf_transformations.quaternion_matrix(quat)[
                :3, :3
            ]  # We only need the 3x3 rotation part

            # The Jacobian for linear velocity is just the rotation matrix
            J = rot_matrix

            # Extract the linear velocity covariance (3x3)
            linear_cov = np.array(twist_with_cov.covariance).reshape(6, 6)[:3, :3]

            # Transform the covariance
            transformed_linear_cov = J @ linear_cov @ J.T

            # Create a new TwistWithCovariance
            transformed_twist_with_cov = TwistWithCovariance()
            transformed_twist_with_cov.twist.linear = Vector3(
                x=transformed_point.point.x,
                y=transformed_point.point.y,
                z=transformed_point.point.z,
            )
            # Keep the original angular component
            transformed_twist_with_cov.twist.angular = twist_with_cov.twist.angular

            # Update the covariance
            transformed_cov = np.zeros((6, 6))
            transformed_cov[:3, :3] = transformed_linear_cov
            transformed_cov[3:, 3:] = np.array(twist_with_cov.covariance).reshape(6, 6)[
                3:, 3:
            ]  # Keep original angular covariance
            transformed_twist_with_cov.covariance = transformed_cov.flatten().tolist()

            return transformed_twist_with_cov

        except tf2_ros.TransformException as ex:
            self.get_logger().error(f"Could not transform twist with covariance: {ex}")
            return None

    def _unix_to_gps_time(self, unix_time):
        gps_epoch = 315964800  # GPS epoch in Unix time (1980-01-06 00:00:00 UTC)
        gps_time = unix_time - gps_epoch
        gps_week = int(gps_time / 604800)  # 604800 seconds in a week
        time_of_week = gps_time % 604800
        return gps_week, time_of_week

    def _get_utc_time(self, unix_time):
        utc_time = time.gmtime(unix_time)
        return (
            utc_time.tm_year,
            utc_time.tm_mon,
            utc_time.tm_mday,
            utc_time.tm_hour,
            utc_time.tm_min,
            utc_time.tm_sec,
        )
