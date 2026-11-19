# Wireless Device Locator

Locate a Bluetooth Low Energy device or a Wi-Fi access point. Select the device, then click the receiver's position on the floor plan at each stop. After readings from four distinct spots, a blue X marks the estimated location and a dashed outline shows where to search.

On Windows, run [Wireless Device Locator.cmd](<Wireless Device Locator.cmd>) with Python 3.10 or newer on PATH.

On macOS or Linux (Bluetooth only, untested), run `uv run --with-requirements requirements-wireless-device-locator.txt python wireless_device_locator_server.py` and open the address it prints.

![Wi-Fi survey with nine measurement points and an estimated access point on Level 1](docs/wireless-device-locator-wifi-survey.png)

![3D simulation comparing the estimated location with the known transmitter position on Level 2](docs/wireless-device-locator-3d-simulation.png)
