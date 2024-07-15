"""Launches GISNav :term:`core` nodes"""
import os
from typing import Final

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription  # type: ignore
from launch_ros.actions import Node

_PACKAGE_NAME: Final = "gisnav"


def generate_launch_description():
    """Generates shared autopilot agnostic launch description"""
    package_share_dir = get_package_share_directory(_PACKAGE_NAME)

    ld = LaunchDescription()
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="camera_optical_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "1.571",
                "0",
                "1.571",
                "camera_frd",
                "camera_optical",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="gisnav_camera_link_optical_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "1.571",
                "0",
                "1.571",
                "gisnav_camera_link_frd",
                "gisnav_camera_link_optical",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="camera_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "0",
                "0",
                "-3.141",
                "gimbal_0",
                "camera",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="camera_frd_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "0",
                "0",
                "3.141",
                "camera",
                "camera_frd",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="gisnav_camera_link_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "0",
                "0",
                "3.141",
                "gisnav_camera_link",
                "gisnav_camera_link_frd",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="gisnav_map_ned_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "1.5707963267948966",  # yaw   (90 degrees in radians)
                "0",  # pitch (0 degrees in radians)
                "3.141592653589793",  # roll  (180 degrees in radians)
                "gisnav_map",
                "gisnav_map_ned",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="base_link_stabilized_frd_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "0",
                "0",
                "-3.141",
                "base_link_stabilized",
                "base_link_stabilized_frd",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="map_to_odom_static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "map",
                "odom",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="robot_localization",
            name="ekf_global_node",
            namespace="robot_localization",
            executable="ukf_node",
            parameters=[
                os.path.join(package_share_dir, "launch/params/ekf_global_node.yaml")
            ],
        )
    )
    ld.add_action(
        Node(
            package="robot_localization",
            name="ekf_local_node",
            namespace="robot_localization",
            executable="ekf_node",
            parameters=[
                os.path.join(package_share_dir, "launch/params/ekf_local_node.yaml")
            ],
        )
    )
    ld.add_action(
        Node(
            package=_PACKAGE_NAME,
            name="stereo_node",
            namespace=_PACKAGE_NAME,
            executable="stereo_node",
            parameters=[
                os.path.join(package_share_dir, "launch/params/stereo_node.yaml")
            ],
        )
    )
    ld.add_action(
        Node(
            package=_PACKAGE_NAME,
            name="bbox_node",
            namespace=_PACKAGE_NAME,
            executable="bbox_node",
            parameters=[
                os.path.join(package_share_dir, "launch/params/bbox_node.yaml")
            ],
        )
    )
    ld.add_action(
        Node(
            package=_PACKAGE_NAME,
            name="pose_node",
            namespace=_PACKAGE_NAME,
            executable="pose_node",
            parameters=[
                os.path.join(package_share_dir, "launch/params/pose_node.yaml")
            ],
        )
    )
    ld.add_action(
        Node(
            package=_PACKAGE_NAME,
            name="twist_node",
            namespace=_PACKAGE_NAME,
            executable="twist_node",
            parameters=[
                os.path.join(package_share_dir, "launch/params/twist_node.yaml")
            ],
        )
    )
    return ld
