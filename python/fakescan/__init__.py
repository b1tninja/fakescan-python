"""fakescan: in-process fake WSD scanner (native) and its control-plane client."""

from .client import (
    ApiError,
    Activity,
    ActivityEvent,
    Client,
    Job,
    LoopbackInfo,
    Status,
    after_ms,
    after_pages,
    mid_page,
)
from . import registry, tray
from .ui import FeederInfo, ProfileInfo, StatusRow, format_status_table, summary_level
from .manager import Manager
from .registry import DeviceRow, ManagedDevice, ReconcileResult, match_wia_to_devices
from .server import Device, DeviceHandle, Server
from .soap import (
    SoapFault,
    acquire_dib_platen,
    acquire_jfif_pages,
    acquire_pages,
    get_scanner_status,
)
from ._uuids import FAKE_UUID_PREFIX, is_fake_uuid
from .wsd_docs import generate_reference_docs
from .wsd_schema import body_problems, load_schema, parse_schema

__all__ = [
    "FAKE_UUID_PREFIX",
    "is_fake_uuid",
    "after_ms",
    "after_pages",
    "mid_page",
    "ApiError",
    "Activity",
    "ActivityEvent",
    "Client",
    "Device",
    "FeederInfo",
    "ProfileInfo",
    "StatusRow",
    "format_status_table",
    "summary_level",
    "tray",
    "DeviceHandle",
    "Job",
    "LoopbackInfo",
    "Manager",
    "ManagedDevice",
    "DeviceRow",
    "ReconcileResult",
    "match_wia_to_devices",
    "registry",
    "Server",
    "SoapFault",
    "Status",
    "acquire_dib_platen",
    "acquire_jfif_pages",
    "acquire_pages",
    "get_scanner_status",
    "body_problems",
    "generate_reference_docs",
    "load_schema",
    "parse_schema",
    "__version__",
]
__version__ = "0.1.0"
