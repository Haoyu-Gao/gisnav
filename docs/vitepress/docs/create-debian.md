# Creating Debian distributable

The GISNav Compose services Debian package `gisnav-compose` is intended to be installed on the companion computer. The service is a simple script that starts the GISNav core Compose services automatically on system startup.

::: info Todo
A broader overview of how GISNav components are built and distributed, including any CI workflows.

:::

## Prerequisites

<!--@include: ./shared/create-colcon-workspace.md-->

<!--@include: ./shared/clone-to-colcon-workspace.md-->

::: info Todo
The dependency repos like `px4_msgs` or `mavros` are not needed in this case, just `gisnav`.

:::

## Make distributable

Create the `.deb` file:

```bash
cd ~/colcon_ws/src/gisnav/systemd/gisnav-compose
make dist
```

You will then find the file in the below folder:

```bash
~/colcon_ws/src/gisnav/systemd/gisnav-compose/build/gisnav-compose
```
