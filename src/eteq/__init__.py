"""eteq - live video from OmniVision OV780 WiFi inspection cameras.

These cameras (sold as the Gardner Bender eTEQ WIC-100 and many rebrands) create
their own WiFi hotspot and speak a proprietary reliable-UDP protocol instead of
RTSP or HTTP. This package implements that protocol and turns the result into
something an ordinary player or browser can show.

Nothing here requires third-party packages. ffmpeg is optional and only used by
the extra ffplay and MJPEG output modes.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
