[![Watch the GISNav demo video](https://img.youtube.com/vi/JAK2DPZC33w/0.jpg)](https://youtu.be/JAK2DPZC33w)

# Introduction

> **Warning** Do not use this software for real drone flights. This software is untested and has only been demonstrated
> in a software-in-the-loop (SITL) simulation environment.

GISNav is a ROS 2 package that enables map-based visual navigation for airborne drones **in a simulation environment**.

GISNav provides an *accurate* **global** position for an airborne drone by visually comparing frames from the drone's 
nadir-facing camera to a map of the drone's *approximate* global position retrieved from an underlying 
GIS system.

# Mock GPS Example

The below steps demonstrate how GISNav's `MockGPSNode` ROS 2 node enables GNSS-free flight with PX4 Autopilot's 
[Mission mode][1] in a SITL simulation.

You will need to have [NVIDIA Container Toolkit][2] for Docker installed.

[1]: https://docs.px4.io/v1.12/en/flight_modes/mission.html

[2]: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html

## Build and run SITL simulation

```bash
git clone https://github.com/hmakelin/gisnav-docker.git
cd gisnav-docker
docker-compose build --build-arg WITH_GISNAV px4-sitl
```

> **Note** The build for the `px4-sitl` image takes a long time, especially if you are building it for the first time.

Once the `px4-sitl` image has been built, run the `mapserver` and `px4-sitl` services:

```bash
docker-compose up -d mapserver px4-sitl
```

> **Note**: 
> * The `mapserver` container needs to download roughly 1 GB of high-resolution aerial imagery, so it may take some 
>   time until it starts serving the WMS endpoint.
> * The `px4-sitl` container should pop up [Gazebo][4] and [QGroundControl][5] automatically once ran. The Gazebo 
>   window may take several minutes to appear, while QGroundControl should appear in a few seconds after running the 
>   container.

[4]: https://gazebosim.org/home

[5]: https://qgroundcontrol.com/

## Upload flight plan via QGroundControl

Once both the Gazebo and QGroundControl windows have appeared (QGroundControl should show the drone location near San 
Carlos airport), use QGroundControl to upload the sample `~/ksql_airport.plan` flight plan that is included inside the 
Docker container, and then start the mission.

## Simulate GPS failure

> **Warning** Do not attempt this on a real flight - simulation use only.

Wait until the drone has risen to its final mission altitude. You should see a visualization of the GISNav-estimated 
field of view projected on the ground appear. You can then try disabling GPS from the *nsh* console running on the drone
through your [MAVLink Shell][6] *(accessible e.g. through QGroundControl > Analyze Tools > MAVLink Console)*:

```
failure gps off
```

The drone should now continue to complete its mission *GNSS-free* with GISNav substituting for GPS.

You can check if PX4 is receiving the mock GPS position estimates by typing the following in the MAVLink shell:

```
listener sensor_gps
```

If the printed GPS message has a `satellites_used` field value of `255`, your PX4 is receiving the mock GPS node output 
as expected.

[6]: https://docs.px4.io/main/en/debug/mavlink_shell.html#qgroundcontrol

# Documentation

See the [latest developer documentation][7] for information on how to setup a local environment for GISNav development, 
for code examples and API documentation, and for contribution guidelines.

[7]: https://hmakelin.github.io/gisnav

# License

This software is released under the MIT license. See the `LICENSE.md` file for more information.
