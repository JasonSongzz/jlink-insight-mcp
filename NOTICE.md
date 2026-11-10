# Attribution / 来源说明

JLink Insight MCP 0.2.0 is based on cyj0920/jlink_mcp, upstream revision
264a4bf4d84b2269cc8a942a82fd98c2ad066477:
https://github.com/cyj0920/jlink_mcp

The upstream MIT license is retained in LICENSE. The original project README is
archived as docs/UPSTREAM_README.md; current usage is documented in README.md.

Added in this distribution: project/ELF inspection, verified deployment,
hardware access reservations, bounded background monitoring, build-artifact
watching, Cortex-M evidence reporting, SystemView wire decoding and tests.

SystemView protocol references:
- https://github.com/SEGGERMicro/SystemView
- https://doc.segger.com/UM08027_SystemView.html

No SEGGER SystemView instrumentation sources are bundled. Obtain those files
from SEGGER and follow the applicable license when integrating into firmware.
J-Link and SystemView are SEGGER product names. This project is not an official
SEGGER product.
