"""This module contains :class:`.PoseNode`, a :term:`ROS` node for estimating
:term:`camera` relative pose between a :term:`query` and :term:`reference` image

The pose is estimated by finding matching keypoints between the query and
reference images and then solving the resulting :term:`PnP` problem.

.. mermaid::
    :caption: :class:`.PoseNode` computational graph

    graph LR
        subgraph PoseNode
            pose[gisnav/pose_node/pose]
        end

        subgraph TransformNode
            image[gisnav/transform_node/image]
        end

        subgraph gscam
            camera_info[camera/camera_info]
        end

        camera_info -->|sensor_msgs/CameraInfo| PoseNode
        image -->|sensor_msgs/Image| PoseNode
        pose -->|geometry_msgs/PoseStamped| MockGPSNode:::hidden
"""
from copy import deepcopy
from typing import Optional, Tuple, Literal

import cv2
import numpy as np
import rclpy
import tf2_ros
import tf_transformations
import torch
from cv_bridge import CvBridge
from kornia.feature import LoFTR
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import CameraInfo, Image, TimeReference
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster
from tf2_ros.transform_broadcaster import TransformBroadcaster
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Odometry

from .. import _messaging as messaging
from .._decorators import ROS, narrow_types
from ..constants import (
    DELAY_DEFAULT_MS,
    MAVROS_TOPIC_TIME_REFERENCE,
    ROS_NAMESPACE,
    ROS_TOPIC_CAMERA_INFO,
    ROS_TOPIC_RELATIVE_PNP_IMAGE,
    ROS_TOPIC_RELATIVE_STEREO_IMAGE,
    STEREO_NODE_NAME,
)


class PoseNode(Node):
    """Solves the keypoint matching and :term:`PnP` problems and publishes the
    solution via ROS transformations library
    """

    CONFIDENCE_THRESHOLD = 0.7
    """Confidence threshold for filtering out bad keypoint matches"""

    MIN_MATCHES = 20
    """Minimum number of keypoint matches before attempting pose estimation"""

    def __init__(self, *args, **kwargs):
        """Class initializer

        :param args: Positional arguments to parent :class:`.Node` constructor
        :param kwargs: Keyword arguments to parent :class:`.Node` constructor
        """
        super().__init__(*args, **kwargs)
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Initialize DL model for map matching (global 3D position)
        self._model = LoFTR(pretrained="outdoor")
        self._model.to(self._device)

        # Initialize ORB detector and brute force matcher for VO
        # (relative position/velocity)
        self._orb = cv2.ORB_create()
        self._bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

        self._cv_bridge = CvBridge()

        # initialize subscription
        self.camera_info
        self.image
        self.image_vo
        self.time_reference

        # Initialize the transform broadcaster
        self.broadcaster = TransformBroadcaster(self)
        self.static_broadcaster = StaticTransformBroadcaster(self)
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)

        # Pinhole camera model camera from to ROS 2 camera_optical (z-axis forward
        # along optical axis) frame i.e. create a quaternion that represents
        # the transformation from a coordinate frame where x points right, y points
        # down, and z points backwards to the ROS 2 convention
        # (REP 103 https://www.ros.org/reps/rep-0103.html#id21) where x points
        # forward, y points left, and z points up
        # TODO: is PoseNode the appropriate place to publish this transform?
        q = (0.5, -0.5, -0.5, -0.5)
        header = messaging.create_header(
            self, "", self.time_reference
        )  # time reference is not important here
        transform_camera = messaging.create_transform_msg(
            header.stamp, "camera_pinhole", "camera", q, np.zeros(3)
        )
        self.static_broadcaster.sendTransform([transform_camera])

        self._previous_pose_previous_query: Optional[PoseStamped] = None

    @property
    @ROS.subscribe(
        MAVROS_TOPIC_TIME_REFERENCE,
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def time_reference(self) -> Optional[TimeReference]:
        """:term:`FCU` time reference via :term:`MAVROS`"""

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
        preprocessed = self.preprocess(msg)
        inferred = self.inference(preprocessed)
        if inferred is not None:
            pose_stamped = self.postprocess(inferred, "Deep match / global position (GIS)")
        else:
            return None

        if pose_stamped is None:
            return None

        r, t = pose_stamped

        rotation_4x4 = np.eye(4)
        rotation_4x4[:3, :3] = r
        try:
            q = tf_transformations.quaternion_from_matrix(rotation_4x4)
        except np.linalg.LinAlgError:
            self.get_logger().warning(
                "image_cb: Could not compute quaternion from estimated rotation. "
                "Returning None."
            )
            return None

        camera_pos = (-r.T @ t).squeeze()
        camera_pos[2] = -camera_pos[
            2
        ]  # todo: implement cleaner way of getting camera position right

        # TODO: implicit assumption that image message here has timestamp in system time
        time_reference = self.time_reference
        if time_reference is None:
            self.get_logger().warning(
                "Publishing world to camera_pinhole transformation without time "
                "reference."
            )
            stamp = msg.header.stamp
        else:
            stamp = (
                rclpy.time.Time.from_msg(msg.header.stamp)
                - (
                    rclpy.time.Time.from_msg(time_reference.header.stamp)
                    - rclpy.time.Time.from_msg(time_reference.time_ref)
                )
            ).to_msg()
        transform_camera = messaging.create_transform_msg(
            stamp, "world", "camera_pinhole", q, camera_pos
        )
        pose_stamped = messaging.transform_to_pose(transform_camera, "world")

        # TODO: implement this as a computed property, not a method
        self.pose(pose_stamped)

        self.broadcaster.sendTransform([transform_camera])

        debug_msg = messaging.get_transform(
            self, "world", "camera_pinhole", rclpy.time.Time()
        )

        debug_ref_image = self._cv_bridge.imgmsg_to_cv2(
            deepcopy(msg), desired_encoding="passthrough"
        )

        # The child frame is the 'camera' frame of the PnP problem as
        # defined here: https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html
        if debug_msg is not None and self.camera_info is not None:
            debug_ref_image = debug_ref_image[:, :, 1]  # second channel is world
            messaging.visualize_transform(
                debug_msg,
                debug_ref_image,
                self.camera_info.height,
                "Camera position in world frame",
            )

    @ROS.publish(
        "~/camera/pose_stamped",
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def pose(self, msg: PoseStamped) -> Optional[PoseStamped]:
        """Camera pose in world frame"""
        # TODO fix this implementation - make derived/computed property not method
        return msg

    @ROS.publish(
        "~/camera/vo/odometry",
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def odometry(self, msg: Odometry) -> Optional[Odometry]:
        """Odometry in odom frame"""
        # TODO convert odometry to a timestamped reference frame and fix scaling (meters) for odom frame
        # TODO fix this implementation - make derived/computed property not method
        return msg

    @ROS.publish(
        "~/camera/vo/pose_stamped",
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def pose_previous_query(self, msg: PoseStamped) -> Optional[PoseStamped]:
        """Camera pose in previous_query frame"""
        # TODO fix this implementation - make derived/computed property not method
        return msg

    @property
    @ROS.max_delay_ms(DELAY_DEFAULT_MS)
    @ROS.subscribe(
        f"/{ROS_NAMESPACE}"
        f'/{ROS_TOPIC_RELATIVE_PNP_IMAGE.replace("~", STEREO_NODE_NAME)}',
        QoSPresetProfiles.SENSOR_DATA.value,
        callback=_image_cb,
    )
    def image(self) -> Optional[Image]:
        """:term:`Query <query>`, :term:`reference`, and :term:`elevation` image
        in a single 4-channel :term:`stack`. The query image is in the first
        channel, the reference image is in the second, and the elevation reference
        is in the last two (sum them together to get a 16-bit elevation reference).


        .. note::
            The existing :class:`sensor_msgs.msg.Image` message is repurposed
            to represent a stereo couple with depth information (technically a
            triplet) to avoid having to introduce custom messages that would
            have to be distributed in a separate package. It will be easier to
            package this later as a rosdebian if everything is already in the
            rosdep index.
        """

    def _image_vo_cb(self, msg: Image) -> None:
        # TODO: use sift to match keypoints, then get pose just like for other image
        """Callback for :attr:`.image` message"""
        preprocessed = self.preprocess(msg, shallow_inference=True)
        inferred = self.inference(preprocessed, shallow_inference=True)
        pose_stamped = self.postprocess(inferred, "Shallow match / relative position (monocular VO)")

        if pose_stamped is None:
            return None

        r, t = pose_stamped

        camera_pos = (-r.T @ t).squeeze()
        #camera_pos[2] = -camera_pos[
        #    2
        #]  # todo: implement cleaner way of getting camera position right

        rotation_4x4 = np.eye(4)
        rotation_4x4[:3, :3] = r
        try:
            q = tf_transformations.quaternion_from_matrix(rotation_4x4)
        except np.linalg.LinAlgError:
            self.get_logger().warning(
                "image_cb: Could not compute quaternion from estimated rotation. "
                "Returning None."
            )
            return None
        # TODO: implicit assumption that image message here has timestamp in system time
        time_reference = self.time_reference
        if time_reference is None:
            self.get_logger().warning(
                "Publishing world to camera_pinhole transformation without time "
                "reference."
            )
            stamp = msg.header.stamp
        else:
            # TODO: make sure we do not get negative time here
            stamp = (
                    rclpy.time.Time.from_msg(msg.header.stamp)
                    - (
                            rclpy.time.Time.from_msg(time_reference.header.stamp)
                            - rclpy.time.Time.from_msg(time_reference.time_ref)
                    )
            ).to_msg()

        # TODO: should use previous query frame timestamp here, not current
        frame_id_timestamped = (
                f"query"
                f"_{stamp.sec}"
                f"_{stamp.nanosec}"
            )
        camera_pose = messaging.create_pose_msg(
            stamp, frame_id_timestamped, q, camera_pos
        )
        self.pose_previous_query(camera_pose)
        if self._previous_pose_previous_query is not None:
            # Publish query_timestamp to query_timestamp transform
            # publish pose
            # publish odometry
            # publish query_timestamp to camera_fru(pinhole) transform
            transform = messaging.pose_to_transform(
                camera_pose,
                self._previous_pose_previous_query.header.frame_id,  # todo remove this input argument, should not edit header/frame_id
                "camera_pinhole"  # camera_pinhole is the camera_rfu frame?
            )
            self.broadcaster.sendTransform([transform])  # query to camera_rfu

            pose_diff = messaging.pose_stamped_diff(self._previous_pose_previous_query, camera_pose)
            transform_diff = messaging.pose_to_transform(pose_diff, frame_id_timestamped, frame_id_timestamped)  # todo get previous query frame timestmap
            self.broadcaster.sendTransform([transform_diff])

            # Send also a timestamped version of previous_query frame
            # Assume the image msg has the timestamp of the previous query frame not current
            transform.header.frame_id = (
                f"{transform.header.frame_id}"
                f"_{transform.header.stamp.sec}"
                f"_{transform.header.stamp.nanosec}"
            )
            self.broadcaster.sendTransform([transform])

            # Todo: come up with an error model to estimate covariances, e.g. us
            #  some simple empirical model based on number and confidence of matches,
            #  or dynamically adjust at runtime using a filtering algorithm

            # Todo 2: Convert to reference (ENU) frame and scale to meters. Fix the
            #  first scaled reference_ts frame used for this as the "odom" frame. Reference
            #  frames are ENU but need scaling to meters. Also save the M matrix used
            #  to convert this to WGS 84. Eventually consider publishing a transform
            #  to earth frame, and then separately an earth frame to WGS 84 transform
            #  which is constant.

            # Todo need to get pose in any one of the timestamped (earth fixed) reference frames
            query_to_odom = messaging.get_transform(
                self,
                "odom",
                "query",
                rclpy.time.Time(),
            )
            assert query_to_odom is not None  # TODO this could be None?

            # TODO this is a non rigid transform so cannot use tf here - cache the
            #  query frame to reference frame transform in this node - match timestamps
            #  on the query frame transform
            pose_current = self.tf_buffer.transform(query_to_odom, "reference")
            previous_query_to_odom = messaging.get_transform(
                self,
                "odom",
                self._previous_pose_previous_query.header.frame_id,
                rclpy.time.Time(),
            )
            pose_previous = self.tf_buffer.transform(previous_query_to_odom, "reference")
            odometry_msg = messaging.pose_stamped_diff_to_odometry(pose_previous, pose_current)
            self.odometry(odometry_msg)
        self._previous_pose_previous_query = camera_pose  # TODO fix: should not be separated from publishing

        # TODO: this is camera pose in previous_query frame, not a transform
        #  between the two frames - needs fixing.
        #transform = messaging.create_transform_msg(
        #    stamp, "previous_query", "query", q, camera_pos
        #)
        #self.broadcaster.sendTransform([transform])


        # Send also a timestamped version of previous_query frame
        # Assume the image msg has the timestamp of the previous query frame not current
        #transform.child_frame_id = (
        #    f"{transform.child_frame_id}"
        #    f"_{msg.header.stamp.sec}"
        #    f"_{msg.header.stamp.nanosec}"
        #)
        #self.broadcaster.sendTransform([transform])

        # TODO: send pose/odometry message (convert to a fixed ENU reference/odom frame
        #  and scale to meters using GISNode published transform value at M[2, 2]

    @property
    @ROS.max_delay_ms(DELAY_DEFAULT_MS)
    @ROS.subscribe(
        f"/{ROS_NAMESPACE}"
        f'/{ROS_TOPIC_RELATIVE_STEREO_IMAGE.replace("~", STEREO_NODE_NAME)}',
        QoSPresetProfiles.SENSOR_DATA.value,
        callback=_image_vo_cb,
    )
    def image_vo(self) -> Optional[Image]:
        """Image pair consisting of query image and reference image for :term:`VO` use.

        .. note::
            Images are set side by side - the first or left image is the current (query)
            image, while the second or right image is the previous (reference) image.
        """

    @property
    @ROS.max_delay_ms(DELAY_DEFAULT_MS)
    @ROS.subscribe(
        f"/{ROS_NAMESPACE}"
        f'/{ROS_TOPIC_RELATIVE_PNP_IMAGE.replace("~", STEREO_NODE_NAME)}',
        QoSPresetProfiles.SENSOR_DATA.value,
        callback=_image_cb,
    )
    def image(self) -> Optional[Image]:
        """:term:`Query <query>`, :term:`reference`, and :term:`elevation` image
        in a grayscale image. The query image is the first (left) image, and the reference
        image is the second (right) image.

        .. note::
            The existing :class:`sensor_msgs.msg.Image` message is repurposed
            to represent a stereo couple to avoid having to introduce custom messages
            that would have to be distributed in a separate package. It will be easier to
            package this later as a rosdebian if everything is already in the
            rosdep index.
        """

    @narrow_types
    def preprocess(
        self, image_quad: Image, shallow_inference: bool = False,
    ) -> Tuple[dict, np.ndarray, np.ndarray, np.ndarray]:
        """Converts incoming 4-channel image to torch tensors

        :param image_quad: A 4-channel image where the first channel is the
            :term:`query`, the second channel is the 8-bit
            :term:`elevation reference`, and the last two channels combined
            represent the 16-bit :term:`elevation reference`.
        :param shallow_inference: If set to True, prepare for faster matching method
            suitable for visual odometry (VO) instead of for deep learning model
        """
        # Convert the ROS Image message to an OpenCV image
        full_image_cv = self._cv_bridge.imgmsg_to_cv2(
            image_quad, desired_encoding="passthrough"
        )

        if not shallow_inference:
            # Check that the image has 4 channels
            channels = full_image_cv.shape[2]
            assert channels == 4, "The image must have 4 channels"

            # Extract individual channels
            query_img = full_image_cv[:, :, 0]
            reference_img = full_image_cv[:, :, 1]
            elevation_16bit_high = full_image_cv[:, :, 2]
            elevation_16bit_low = full_image_cv[:, :, 3]

            # Reconstruct 16-bit elevation from the last two channels
            reference_elevation = (
                elevation_16bit_high.astype(np.uint16) << 8
            ) | elevation_16bit_low.astype(np.uint16)

            # Optionally display images
            # self._display_images("Query", query_img, "Reference", reference_img)

            if torch.cuda.is_available():
                qry_tensor = torch.Tensor(query_img[None, None]).cuda() / 255.0
                ref_tensor = torch.Tensor(reference_img[None, None]).cuda() / 255.0
            else:
                qry_tensor = torch.Tensor(query_img[None, None]) / 255.0
                ref_tensor = torch.Tensor(reference_img[None, None]) / 255.0

            return (
                {"image0": qry_tensor, "image1": ref_tensor},
                query_img,
                reference_img,
                reference_elevation,
            )
        else:
            # TODO: Define a return type or make this method cleaner in some other way -
            #  the VO/shallow branch of this method is like a completely different method!
            h, w = full_image_cv.shape  # should only have 2 dimensions
            half_w = int(w/2)
            query_img = full_image_cv[:, :half_w]  # w should be divisible by 2
            reference_img = full_image_cv[:, half_w:]
            return None, query_img, reference_img, np.zeros_like(reference_img)  # None for tensors - not used

    @staticmethod
    def _display_images(*args):
        """Displays images using OpenCV"""
        for i in range(0, len(args), 2):
            cv2.imshow(args[i], args[i + 1])
        cv2.waitKey(1)

    def inference(self, preprocessed_data, shallow_inference: bool = False):
        """Do keypoint matching."""
        if not shallow_inference:
            with torch.no_grad():
                results = self._model(preprocessed_data[0])

            conf = results["confidence"].cpu().numpy()
            good = conf > self.CONFIDENCE_THRESHOLD
            mkp_qry = results["keypoints0"].cpu().numpy()[good, :]
            mkp_ref = results["keypoints1"].cpu().numpy()[good, :]

            return mkp_qry, mkp_ref, *preprocessed_data[1:]
        else:
            # find the keypoints and descriptors with ORB
            _, qry, ref, elevation = preprocessed_data

            kp_qry, desc_qry = self._orb.detectAndCompute(qry, None)
            kp_ref, desc_ref = self._orb.detectAndCompute(ref, None)

            matches = self._bf.knnMatch(desc_qry, desc_ref, k=2)

            # Apply ratio test
            good = []
            for m, n in matches:
                # TODO: have a separate confidence threshold for shallow and deep matching?
                if m.distance < self.CONFIDENCE_THRESHOLD * n.distance:
                    good.append(m)

            # TODO define common match format (here we have cv2.DMatch) but deep version
            # has tuples/lists?

            mkps = list(
                map(
                    lambda dmatch: (
                        tuple(kp_qry[dmatch.queryIdx].pt),
                        tuple(kp_ref[dmatch.trainIdx].pt)
                    ),
                    good
                )
            )

            if len(mkps) > 0:
                mkp_qry, mkp_ref = zip(*mkps)
            else:
                return None
            mkp_qry = np.array(mkp_qry)
            mkp_ref = np.array(mkp_ref)

            return mkp_qry, mkp_ref, qry, ref, elevation


    def postprocess(self, inferred_data, label) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Filters matches based on confidence threshold and calculates :term:`pose`"""

        @narrow_types(self)
        def _postprocess(
            camera_info: CameraInfo, inferred_data: Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray], label: str
        ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
            mkp_qry, mkp_ref, query_img, reference_img, elevation = inferred_data

            if mkp_qry is None or len(mkp_qry) < self.MIN_MATCHES:
                return None

            k_matrix = camera_info.k.reshape((3, 3))

            mkp2_3d = self._compute_3d_points(mkp_ref, elevation)
            # Adjust y-axis for ROS convention (origin is bottom left, not top left),
            # elevation (z) coordinate remains unchanged
            mkp2_3d[:, 1] = camera_info.height - mkp2_3d[:, 1]
            mkp_qry[:, 1] = camera_info.height - mkp_qry[:, 1]
            r, t = self._compute_pose(mkp2_3d, mkp_qry, k_matrix)

            self._visualize_matches_and_pose(
                query_img.copy(), reference_img.copy(), mkp_qry, mkp_ref, k_matrix, r, t, label
            )

            return r, t

        return _postprocess(self.camera_info, inferred_data, label)

    @staticmethod
    def _compute_3d_points(mkp_ref, elevation):
        """Computes 3D points from matches"""
        if elevation is None:
            return np.hstack((mkp_ref, np.zeros((len(mkp_ref), 1))))

        x, y = np.transpose(np.floor(mkp_ref).astype(int))
        z_values = elevation[y, x].reshape(-1, 1)
        return np.hstack((mkp_ref, z_values))

    @staticmethod
    def _compute_pose(mkp2_3d, mkp_qry, k_matrix):
        """Computes :term:`pose` using :func:`cv2.solvePnPRansac`"""
        dist_coeffs = np.zeros((4, 1))
        _, r, t, _ = cv2.solvePnPRansac(
            mkp2_3d,
            mkp_qry,
            k_matrix,
            dist_coeffs,
            useExtrinsicGuess=False,
            iterationsCount=10,
        )
        r_matrix, _ = cv2.Rodrigues(r)
        return r_matrix, t

    def _visualize_matches_and_pose(self, qry, ref, mkp_qry, mkp_ref, k, r, t, label):
        """Visualizes matches and projected :term:`FOV`"""

        # We modify these from ROS to cv2 axes convention so we create copies
        mkp_qry = mkp_qry.copy()
        mkp_ref = mkp_ref.copy()

        h_matrix = k @ np.delete(np.hstack((r, t)), 2, 1)
        projected_fov = self._project_fov(qry, h_matrix)

        # Invert the y-coordinate, considering the image height (input r and t
        # are in ROS convention where origin is at bottom left of image, we
        # want origin to be at top left for cv2
        h = self.camera_info.height
        mkp_ref[:, 1] = mkp_ref[:, 1]
        mkp_qry[:, 1] = h - mkp_qry[:, 1]

        projected_fov[:, :, 1] = h - projected_fov[:, :, 1]
        img_with_fov = cv2.polylines(
            ref, [np.int32(projected_fov)], True, 255, 3, cv2.LINE_AA
        )

        mkp_qry = [cv2.KeyPoint(x[0], x[1], 1) for x in mkp_qry]
        mkp_ref = [cv2.KeyPoint(x[0], x[1], 1) for x in mkp_ref]

        matches = [cv2.DMatch(i, i, 0) for i in range(len(mkp_qry))]

        match_img = cv2.drawMatches(
            img_with_fov,
            mkp_ref,
            qry,
            mkp_qry,
            matches,
            None,
            matchColor=(0, 255, 0),
            flags=2,
        )

        cv2.imshow(label, match_img)
        cv2.waitKey(1)

    @staticmethod
    def _project_fov(img, h_matrix):
        """Projects :term:`FOV` on :term:`reference` image"""
        height, width = img.shape[0:2]
        src_pts = np.float32(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
        ).reshape(-1, 1, 2)
        try:
            return cv2.perspectiveTransform(src_pts, np.linalg.inv(h_matrix))
        except np.linalg.LinAlgError:
            return src_pts
