"""This module contains :class:`.StereoNode`, a :term:`ROS` node generating the
:term:`query` and :term:`reference` stereo image pair by either rotating the reference
image based on :term:`vehicle` heading and then cropping it based on the
:term:`camera` information, or by coupling to successive image frames from the monocular
onboard camera.
"""
from typing import Final, Optional, Tuple

import cv2
import numpy as np
import rclpy
import tf2_ros
import tf_transformations
from cv_bridge import CvBridge
from geometry_msgs.msg import TransformStamped
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import CameraInfo, Image
from std_msgs.msg import Header

from gisnav_msgs.msg import MonocularStereoImage  # type: ignore[attr-defined]

from .. import _transformations as tf_
from .._decorators import ROS, narrow_types
from ..constants import (
    GIS_NODE_NAME,
    ROS_NAMESPACE,
    ROS_TOPIC_CAMERA_INFO,
    ROS_TOPIC_IMAGE,
    ROS_TOPIC_RELATIVE_ORTHOIMAGE,
    ROS_TOPIC_RELATIVE_POSE_IMAGE,
    ROS_TOPIC_RELATIVE_TWIST_IMAGE,
)


class StereoNode(Node):
    """Generates and publishes a synthetic :term:`query` and :term:`reference` stereo
    image couple. Synthetic refers to the fact that no stereo camera is actually assumed
    or required. The reference can be an older image from the same monocular camera, or
    alternatively an aligned map raster from the GIS server.
    """

    _ROS_PARAM_DESCRIPTOR_READ_ONLY: Final = ParameterDescriptor(read_only=True)
    """A read only ROS parameter descriptor"""

    def __init__(self, *args, **kwargs) -> None:
        """Class initializer

        :param args: Positional arguments to parent :class:`.Node` constructor
        :param kwargs: Keyword arguments to parent :class:`.Node` constructor
        """
        super().__init__(*args, **kwargs)

        # Converts image_raw to cv2 compatible image
        self._cv_bridge = CvBridge()

        # Calling these decorated properties the first time will setup
        # subscriptions to the appropriate ROS topics
        self.orthoimage
        self.camera_info
        self.image

        # TODO Declare as property?
        self.previous_image: Optional[Image] = None

        # setup publisher to pass launch test without image callback being
        # triggered
        self.pose_image
        self.twist_image

        # Initialize the transform broadcaster and listener
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

    @property
    @ROS.subscribe(
        f"/{ROS_NAMESPACE}"
        f'/{ROS_TOPIC_RELATIVE_ORTHOIMAGE.replace("~", GIS_NODE_NAME)}',
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def orthoimage(self) -> Optional[Image]:
        """Subscribed :term:`orthoimage` for :term:`pose` estimation"""

    @property
    # @ROS.max_delay_ms(messaging.DELAY_SLOW_MS) - gst plugin does not enable timestamp?
    @ROS.subscribe(
        ROS_TOPIC_CAMERA_INFO,
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def camera_info(self) -> Optional[CameraInfo]:
        """Camera info for determining appropriate :attr:`.orthoimage` resolution"""

    def _image_cb(self, msg: Image) -> None:
        """Callback for :attr:`.image` message"""
        self.pose_image  # publish rotated and cropped orthoimage stack
        self.twist_image  # publish two subsequent images for VO

        # TODO this is brittle - nothing is enforcing that this is assigned after
        #  publishing stereo_image
        self.previous_image = (
            msg  # needed for VO - leave this for last in this callback
        )

    @property
    # @ROS.max_delay_ms(messaging.DELAY_FAST_MS) - gst plugin does not enable timestamp?
    @ROS.subscribe(
        ROS_TOPIC_IMAGE,
        QoSPresetProfiles.SENSOR_DATA.value,
        callback=_image_cb,
    )
    def image(self) -> Optional[Image]:
        """Raw image data from vehicle camera for pose estimation"""

    @ROS.transform(invert=False)  # , add_timestamp=True) timestamp added manually
    def _world_to_reference_transform(
        self,
        M: np.ndarray,
        header_msg: Header,
        header_reference: Header,
    ) -> Optional[TransformStamped]:
        @narrow_types(self)
        def _transform(
            M: np.ndarray,
            header_msg: Header,
            header_reference: Header,
        ) -> Optional[TransformStamped]:
            # 3D version of the inverse rotation and cropping transform
            M_3d = np.eye(4)
            M_3d[:2, :2] = M[:2, :2]
            M_3d[:2, 3] = M[:2, 2]

            t = M_3d[:3, 3]

            try:
                q = tf_transformations.quaternion_from_matrix(M_3d)
            except np.linalg.LinAlgError:
                self.get_logger().warning(
                    "_pnp_image: Could not compute quaternion from estimated rotation. "
                    "Returning None."
                )
                return None

            transform_msg = tf_.create_transform_msg(
                header_msg.stamp,
                header_msg.frame_id,
                header_reference.frame_id,
                q,
                t,
            )

            # TODO clean this up
            M = tf_.proj_to_affine(header_reference.frame_id)
            # Flip x and y in between to make this transformation chain work
            T = np.array([[0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
            compound_transform = M @ T @ np.linalg.inv(M_3d)
            proj_str = tf_.affine_to_proj(compound_transform)
            transform_msg.header.frame_id = proj_str

            return transform_msg

        return _transform(M, header_msg, header_reference)

    @property
    @ROS.publish(
        ROS_TOPIC_RELATIVE_POSE_IMAGE,
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def pose_image(self) -> Optional[Image]:
        """Published :term:`stacked <stack>` image consisting of query image,
        reference image, and optional reference elevation raster (:term:`DEM`).

        .. note::
            Semantically not a single image, but a stack of two 8-bit grayscale
            images and one 16-bit "image-like" elevation reference, stored in a
            compact way in an existing message type so to avoid having to also
            publish custom :term:`ROS` message definitions.
        """

        @narrow_types(self)
        def _pnp_image(
            image: Image,
            orthoimage: Image,
            transform: TransformStamped,
        ) -> Optional[Image]:
            """Rotate and crop and orthoimage stack to align with query image"""
            transform = transform.transform

            query_img = self._cv_bridge.imgmsg_to_cv2(image, desired_encoding="mono8")

            orthoimage_stack = self._cv_bridge.imgmsg_to_cv2(
                orthoimage, desired_encoding="passthrough"
            )

            assert orthoimage_stack.shape[2] == 3, (
                f"Orthoimage stack channel count was {orthoimage_stack.shape[2]} "
                f"when 3 was expected (one channel for 8-bit grayscale reference "
                f"image and two 8-bit channels for 16-bit elevation reference)"
            )

            # Rotate and crop orthoimage stack
            # TODO: implement this part better e.g. use
            #  tf_transformations.euler_from_quaternion
            camera_yaw_degrees = tf_.extract_yaw(transform.rotation)
            camera_roll_degrees = tf_.extract_roll(transform.rotation)
            # This is assumed to be positive clockwise when looking down nadir
            # (z axis up in an ENU frame), z is aligned with zenith so in that sense
            # this is positive in the counter-clockwise direction. E.g. east aligned
            # rotation is positive 90 degrees.
            rotation = (camera_yaw_degrees + camera_roll_degrees) % 360

            crop_shape: Tuple[int, int] = query_img.shape[0:2]

            # here positive rotation is counter-clockwise, so we invert
            orthoimage_rotated_stack, M = self._rotate_and_crop_center(
                orthoimage_stack, rotation, crop_shape
            )

            # Add query image on top to complete full image stack
            pnp_image_stack = np.dstack((query_img, orthoimage_rotated_stack))

            pnp_image_msg = self._cv_bridge.cv2_to_imgmsg(
                pnp_image_stack, encoding="passthrough"
            )

            # Use orthoimage timestamp in frame but not in message
            # (otherwise transform message gets too old
            pnp_image_msg.header.stamp = image.header.stamp

            # Publish transformation
            transform = self._world_to_reference_transform(
                np.linalg.inv(M),  # TODO: try-except
                pnp_image_msg.header,
                orthoimage.header,
            )

            pnp_image_msg.header.frame_id = transform.header.frame_id

            return pnp_image_msg

        query_image, orthoimage = self.image, self.orthoimage

        # Need camera orientation in an ENU frame ("map") to rotate
        # the orthoimage stack
        transform = (
            tf_.get_transform(self, "map", "camera", rclpy.time.Time())
            if hasattr(self, "_tf_buffer")
            else None
        )

        return _pnp_image(
            query_image,
            orthoimage,
            transform,
        )

    @property
    @ROS.publish(
        ROS_TOPIC_RELATIVE_TWIST_IMAGE,
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def twist_image(self) -> Optional[MonocularStereoImage]:
        """Published stereo couple image consisting of query image and reference image
        for :term:`VO` use.
        """

        @narrow_types(self)
        def _stereo_image(qry: Image, ref: Image) -> Optional[MonocularStereoImage]:
            return MonocularStereoImage(query=qry, reference=ref)

        return _stereo_image(qry=self.image, ref=self.previous_image)

    # @staticmethod
    def _rotate_and_crop_center(
        self, image: np.ndarray, angle_degrees: float, shape: Tuple[int, int]
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Rotates an image around its center axis and then crops it to the
        specified shape.

        :param image: Numpy array representing the image.
        :param angle: Rotation angle in degrees.
        :param shape: Tuple (height, width) representing the desired shape
            after cropping.
        :return: Tuple of 1. Cropped and rotated image, and 2. matrix that can be
            used to convert points in rotated and cropped frame back into original
            frame
        """
        # Image dimensions
        h, w = image.shape[:2]

        # Center of rotation
        center = (w // 2, h // 2)

        # Calculate the rotation matrix
        rotation_matrix = cv2.getRotationMatrix2D(center, angle_degrees, 1.0)

        # Perform the rotation
        rotated_image = cv2.warpAffine(image, rotation_matrix, (w, h))

        # Calculate the cropping coordinates
        dx = center[0] - shape[1] // 2
        dy = center[1] - shape[0] // 2

        # Perform the cropping
        cropped_image = rotated_image[dy : dy + shape[0], dx : dx + shape[1]]

        # Invert the matrix
        extended_matrix = np.vstack([rotation_matrix, [0, 0, 1]])
        inverse_matrix = np.linalg.inv(extended_matrix)

        # Center-crop inverse translation
        T = np.array([[1, 0, dx], [0, 1, dy], [0, 0, 1]])

        inverse_matrix = inverse_matrix @ T

        # refimg = self._cv_bridge.imgmsg_to_cv2(
        #    self.orthoimage, desired_encoding="passthrough"
        # ).copy()
        # br = inverse_matrix @ np.array([640, 360, 1])
        # tf_.visualize_camera_corners(
        #    refimg,
        #    [inverse_matrix @ np.array([0, 0, 1]), br],
        #    "World frame origin/top-left position in reference frame",
        # )

        return cropped_image, inverse_matrix
