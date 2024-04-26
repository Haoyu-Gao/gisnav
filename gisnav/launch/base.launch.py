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
            name="static_broadcaster",
            arguments=[
                "0",
                "0",
                "0",
                "-1.571",
                "0",
                "-1.571",
                "camera",
                "camera_optical",
            ],
        ),
    )
    ld.add_action(
        Node(
            package="robot_localization",
            name="ekf_node",
            namespace="robot_localization",
            executable="ekf_node",
            parameters=[os.path.join(package_share_dir, "launch/params/ekf_node.yaml")],
        )
    )
    ld.add_action(
        Node(
            package=_PACKAGE_NAME,
            name="gis_node",
            namespace=_PACKAGE_NAME,
            executable="gis_node",
            parameters=[os.path.join(package_share_dir, "launch/params/gis_node.yaml")],
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
    return ld
