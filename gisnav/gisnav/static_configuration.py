"""This module contains the static configuration of the :term:`ROS` namespace
and node and topic names

Using this modulenodes that talk to each other can refer to a single source of truth
"""
from typing import Final

ROS_NAMESPACE: Final = "gisnav"
""":term:`ROS` node namespace"""

GIS_NODE_NAME: Final = "gis_node"
"""Name of :class:`.GISNode` spun up by :func:`.run_gis_node`"""

CV_NODE_NAME: Final = "cv_node"
"""Name of :class:`.CVNode` spun up by :func:`.run_cv_node`"""

MOCK_GPS_NODE_NAME: Final = "mock_gps_node"
"""Name of :class:`.MockGPSNode` spun up by :func:`.run_mock_gps_node`"""

RVIZ_NODE_NAME: Final = "rviz_node"
"""Name of :class:`.RVizNode` spun up by :func:`.run_rviz_node`"""

ROS_TOPIC_RELATIVE_VEHICLE_GEOPOSE: Final = "~/vehicle/geopose"
"""Relative :term:`topic` into which :class:`.GISNode` publishes
:attr:`.GISNode.vehicle_geopose`."""

ROS_TOPIC_RELATIVE_VEHICLE_ALTITUDE: Final = "~/vehicle/altitude"
"""Relative :term:`topic` into which :class:`.GISNode` publishes
:attr:`.GISNode.vehicle_altitude`.
"""

ROS_TOPIC_RELATIVE_GROUND_TRACK_GEOPOSE: Final = "~/ground_track/geopose"
"""Relative :term:`topic` into which :class:`.GISNode` publishes
:attr:`.GISNode.ground_track_geopose`.
"""

ROS_TOPIC_RELATIVE_GROUND_TRACK_ELEVATION: Final = "~/ground_track/elevation"
"""Relative :term:`topic` into which :class:`.GISNode` publishes
:attr:`.GISNode.ground_track_elevation`.
"""

ROS_TOPIC_RELATIVE_ORTHOIMAGE: Final = "~/orthoimage"
"""Relative :term:`topic` into which :class:`.GISNode` publishes
:attr:`.GISNode.orthoimage`.
"""

ROS_TOPIC_RELATIVE_CAMERA_QUATERNION: Final = "~/camera/quaternion"
"""Relative :term:`topic` into which :class:`.GISNode` publishes
:attr:`.GISNode.camera_quaternion`.
"""

ROS_TOPIC_RELATIVE_CAMERA_GEOPOSE: Final = "~/camera/geopose"
"""Relative :term:`topic` into which :class:`.GISNode` publishes
:attr:`.GISNode.camera_geopose`.
"""

ROS_TOPIC_RELATIVE_VEHICLE_ESTIMATED_GEOPOSE: Final = "~/vehicle/estimated/geopose"
"""Relative :term:`topic` into which :class:`.CVNode` publishes
:attr:`.CVNode.vehicle_estimated_geopose`.
"""

ROS_TOPIC_RELATIVE_VEHICLE_ESTIMATED_ALTITUDE: Final = "~/vehicle/estimated/altitude"
"""Relative :term:`topic` into which :class:`.CVNode` publishes
:attr:`.CVNode.vehicle_estimated_altitude`.
"""
