"""Launches GISNav with PX4 SITL simulation development configuration"""
import os
from typing import Final

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription  # type: ignore
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import ThisLaunchFileDir
from launch_ros.actions import Node

_PACKAGE_NAME: Final = "gisnav"


def generate_launch_description():
    """Generates launch description with PX4 Fast DDS bridge adapter"""
    package_share_dir = get_package_share_directory(_PACKAGE_NAME)

    ld = LaunchDescription(
        [
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    [ThisLaunchFileDir(), "/default.launch.py"]
                )
            ),
        ]
    )
    ld.add_action(
        Node(
            package=_PACKAGE_NAME,
            name="qgis_node",
            namespace=_PACKAGE_NAME,
            executable="qgis_node",
            parameters=[
                os.path.join(package_share_dir, "launch/params/qgis_node.yaml")
            ],
        )
    )
    return ld
