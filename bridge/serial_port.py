# bridge/serial_port.py
#
# Works out which serial port the panel is on.
#
# The port name is the one genuinely machine-specific thing in this project:
# COM3 on Windows, /dev/ttyUSB0 or /dev/ttyACM0 on Linux, /dev/cu.* on macOS,
# and the number can change between reboots. Rather than hardcode it, look for
# a board by its USB vendor id and let an environment variable win outright.
import os

ENV_VAR = "STATUS_PANEL_PORT"

# USB-serial vendor ids seen on Arduino Uno boards and compatibles.
KNOWN_VIDS = {
    0x2341,  # Arduino
    0x2A03,  # Arduino (.org era)
    0x1A86,  # CH340/CH341 -- common on compatibles, incl. the board used here
    0x0403,  # FTDI
    0x10C4,  # Silicon Labs CP210x
}


def describe(ports):
    if not ports:
        return "  (none found)"
    return "\n".join(f"  {p.device}  {getattr(p, 'description', '') or ''}".rstrip()
                     for p in ports)


def choose_port(ports, override=None):
    """Pick the panel's port from a list of pyserial ListPortInfo objects.

    Pure function so it can be tested without hardware. Raises RuntimeError
    with an actionable message rather than guessing wrong -- opening the wrong
    device is worse than refusing to start.
    """
    if override:
        return override

    ports = list(ports)
    known = [p for p in ports if getattr(p, "vid", None) in KNOWN_VIDS]

    if len(known) == 1:
        return known[0].device
    if len(known) > 1:
        raise RuntimeError(
            "Several boards look like an Arduino; set "
            f"{ENV_VAR} to choose one:\n{describe(known)}"
        )
    if len(ports) == 1:
        # Nothing matched by vendor id, but there's only one candidate.
        return ports[0].device
    if not ports:
        raise RuntimeError(
            "No serial ports found. Check the USB cable, and on Linux confirm "
            "you're in the 'dialout' group (see the README)."
        )
    raise RuntimeError(
        f"Couldn't identify the board among these ports; set {ENV_VAR} to "
        f"the right one:\n{describe(ports)}"
    )


def resolve():
    """Resolve the port to open, consulting the environment first."""
    from serial.tools import list_ports
    return choose_port(list_ports.comports(), os.environ.get(ENV_VAR))
