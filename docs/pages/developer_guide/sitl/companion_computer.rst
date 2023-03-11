Onboard computer
____________________________________________________
This section contains instructions on how to take advantage of Docker composable services to run ``gisnav`` and a
GIS server on an onboard computer while the SITL simulation itself runs on a more powerful (desktop) computer to get a
better idea of performance in a real use case.

Jetson Nano
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

These instructions assume that your Jetson Nano is connected to the same network as your desktop computer. This
could be done e.g. by connecting them to the same WiFi (need dongle for Jetson Nano), or by connecting them by
Ethernet cable. Example instructions with screenshots for both WiFi and Ethernet cable can be found through the
following link:

* https://www.forecr.io/blogs/connectivity/how-to-share-internet-from-computer-to-nvidia-jetson-modules

See the below screenshot:

 .. figure:: ../../../_static/img/gisnav_hil_jetson_nano_setup.jpg

    Jetson Nano connected to laptop via micro-USB and Ethernet. Power supply from wall socket.

Another setup that can be used for SITL and HIL simulation over serial connection without having to neither disconnect
or reconnect anything when switching between SITL/UDP and HIL/serial:

.. figure:: ../../../_static/img/gisnav_hil_fmuk66-e_setup.jpg

    See :ref:`Jetson Nano & Pixhawk` for more information

Log into your desktop computer and build and run the services required for the SITL simulation:

.. code-block:: bash
    :caption: Run Gazebo SITL simulation on desktop

    cd ~/colcon_ws/src/gisnav
    make -C docker build-offboard-sitl-px4
    make -C docker up-offboard-sitl-px4

Then log into your Jetson Nano install `QEMU`_ emulators to make ``linux/amd64`` images run on the ``linux/arm64``
Jetson Nano:

.. code-block:: bash

     docker run --privileged --rm tonistiigi/binfmt --install all

.. _QEMU: https://docs.docker.com/build/building/multi-platform/#building-multi-platform-images

Then build and run the onboard services on the Jetson Nano:

.. code-block:: bash
    :caption: Run GISNav and GIS server on onboard computer

    cd ~/colcon_ws/src/gisnav
    make -C docker build-companion-sitl-px4
    make -C docker up-companion-sitl-px4

You should now have the SITL simulation and QGgroundControl running on your offboard workstation, while ``gisnav``,
``mapserver`` and the autopilot specific middleware run on your Jetson Nano. If you have your network setup correctly,
the middleware on the onboard companion computer will connect to the simulated autopilot on your workstation and pipe
the telemetry and video feed to ROS for GISNav to consume.
