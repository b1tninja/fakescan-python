//! Python bindings for fakescan: the lab's fake WSD scanner, run in-process.
//!
//! `Server` drives the same [`fakescan::server::serve`] the `fakescan` binary runs, on its own
//! thread and tokio runtime, bound to an ephemeral port by default. Control it with the existing
//! HTTP `Client` against `base_url`, or point a WIA device at its `ScannerService` endpoint.

use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::sync::Mutex;
use std::thread::JoinHandle;

use fakescan::server::{serve, DeviceConfig, Ready, ServerConfig};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::sync::oneshot;
use uuid::Uuid;

/// `(id, profile, uuid, friendly_name, initial_pages)` of one `Device(...)`.
type DeviceSpec = (String, String, Option<String>, Option<String>, Option<u32>);

#[pyclass(module = "fakescan")]
struct Server {
    ready: Ready,
    stop: Mutex<Option<oneshot::Sender<()>>>,
    thread: Mutex<Option<JoinHandle<anyhow::Result<()>>>>,
}

#[pymethods]
impl Server {
    /// Start the server and return once it is listening.
    #[new]
    #[pyo3(signature = (*, profiles, profile="brother-wsd", host="127.0.0.1", port=0, device_uuid=None, wsd=true, discovery=false, advertise=None, devices=None, initial_pages=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        profiles: PathBuf,
        profile: &str,
        host: &str,
        port: u16,
        device_uuid: Option<&str>,
        wsd: bool,
        discovery: bool,
        advertise: Option<&str>,
        devices: Option<Vec<DeviceSpec>>,
        initial_pages: Option<u32>,
    ) -> PyResult<Self> {
        let ip: IpAddr = host
            .parse()
            .map_err(|e| PyRuntimeError::new_err(format!("host {host}: {e}")))?;
        let mut cfg = ServerConfig::lab(profiles, profile);
        cfg.listen = SocketAddr::new(ip, port);
        cfg.wsd = wsd;
        cfg.discovery = wsd && discovery;
        cfg.probe_listen = false;
        // An embedded server is one of many test instances: no per-port lock, no named pipe,
        // and Ctrl-C belongs to Python.
        cfg.instance_lock = false;
        cfg.named_pipe = false;
        cfg.ctrl_c = false;
        // Daemon-wide default for devices that set none (a Device's own value wins).
        cfg.initial_pages = initial_pages;
        if let Some(list) = devices.filter(|l| !l.is_empty()) {
            let mut out = Vec::new();
            for (id, prof, uuid, name, pages) in list {
                let uuid = match uuid {
                    Some(t) => Uuid::parse_str(&t)
                        .map_err(|e| PyRuntimeError::new_err(format!("device {id}: uuid: {e}")))?,
                    None => fakescan::lab::random_fake_uuid(),
                };
                let mut d = DeviceConfig::new(id, prof, uuid);
                d.friendly_name = name;
                d.initial_pages = pages;
                out.push(d);
            }
            // The first device is the default one and may keep the lab UUID when none was given.
            cfg.devices = out;
        } else if let Some(text) = device_uuid {
            cfg.devices[0].device_uuid = Uuid::parse_str(text)
                .map_err(|e| PyRuntimeError::new_err(format!("device_uuid: {e}")))?;
        }
        if let Some(text) = advertise {
            cfg.advertise = Some(
                text.parse()
                    .map_err(|e| PyRuntimeError::new_err(format!("advertise {text}: {e}")))?,
            );
        }

        let (ready_tx, ready_rx) = oneshot::channel();
        let (stop_tx, stop_rx) = oneshot::channel();
        let thread = std::thread::Builder::new()
            .name("fakescan-server".into())
            .spawn(move || {
                let runtime = tokio::runtime::Builder::new_multi_thread()
                    .enable_all()
                    .worker_threads(2)
                    .build()?;
                runtime.block_on(serve(cfg, Some(ready_tx), Some(stop_rx)))
            })
            .map_err(|e| {
                PyRuntimeError::new_err(format!("could not start the server thread: {e}"))
            })?;

        match py.allow_threads(|| ready_rx.blocking_recv()) {
            Ok(ready) => Ok(Self {
                ready,
                stop: Mutex::new(Some(stop_tx)),
                thread: Mutex::new(Some(thread)),
            }),
            Err(_) => {
                // The server ended before it was ready: surface why.
                let reason = match thread.join() {
                    Ok(Err(e)) => format!("{e:#}"),
                    Ok(Ok(())) => "exited before listening".to_string(),
                    Err(_) => "panicked while starting".to_string(),
                };
                Err(PyRuntimeError::new_err(format!(
                    "fakescan server failed to start: {reason}"
                )))
            }
        }
    }

    #[getter]
    fn port(&self) -> u16 {
        self.ready.local_addr.port()
    }

    #[getter]
    fn base_url(&self) -> String {
        let ip = self.ready.local_addr.ip();
        let host = if ip.is_unspecified() {
            "127.0.0.1".to_string()
        } else {
            ip.to_string()
        };
        format!("http://{host}:{}", self.ready.local_addr.port())
    }

    #[getter]
    fn device_uuid(&self) -> String {
        self.ready.device_uuid.to_string()
    }

    /// `(id, uuid, profile, friendly_name)` for every device present at start-up.
    #[getter]
    fn devices(&self) -> Vec<(String, String, String, String)> {
        self.ready
            .devices
            .iter()
            .map(|d| {
                (
                    d.id.clone(),
                    d.uuid.to_string(),
                    d.profile.clone(),
                    d.friendly_name.clone(),
                )
            })
            .collect()
    }

    #[getter]
    fn running(&self) -> bool {
        self.thread
            .lock()
            .map(|g| g.as_ref().is_some_and(|t| !t.is_finished()))
            .unwrap_or(false)
    }

    /// Stop serving and wait for the server thread. Safe to call twice.
    fn stop(&self, py: Python<'_>) -> PyResult<()> {
        if let Some(tx) = self.stop.lock().ok().and_then(|mut g| g.take()) {
            let _ = tx.send(());
        }
        let thread = self.thread.lock().ok().and_then(|mut g| g.take());
        if let Some(thread) = thread {
            let result = py.allow_threads(|| thread.join());
            match result {
                Ok(Ok(())) => {}
                Ok(Err(e)) => {
                    return Err(PyRuntimeError::new_err(format!(
                        "fakescan server stopped with an error: {e:#}"
                    )))
                }
                Err(_) => return Err(PyRuntimeError::new_err("fakescan server thread panicked")),
            }
        }
        Ok(())
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    #[pyo3(signature = (*_args))]
    fn __exit__(&self, py: Python<'_>, _args: &Bound<'_, pyo3::types::PyTuple>) -> PyResult<bool> {
        self.stop(py)?;
        Ok(false)
    }

    fn __repr__(&self) -> String {
        format!(
            "Server({}, uuid={})",
            self.base_url(),
            self.ready.device_uuid
        )
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        if let Some(tx) = self.stop.get_mut().ok().and_then(|g| g.take()) {
            let _ = tx.send(());
        }
        // Do not join here: Drop may run while Python holds the GIL; the thread exits on its own.
    }
}

/// Layout-v3 page spec from the Python keyword arguments (JSON object text; `dpi` / `page` defaults).
fn v3_spec(dpi: u32, page: u32, params: Option<&str>) -> PyResult<fakescan::diag_v3::PageSpec> {
    let mut v: serde_json::Value = match params {
        Some(p) if !p.trim().is_empty() => serde_json::from_str(p).map_err(|e| PyRuntimeError::new_err(e.to_string()))?,
        _ => serde_json::json!({}),
    };
    let o = v
        .as_object_mut()
        .ok_or_else(|| PyRuntimeError::new_err("params must be a JSON object"))?;
    o.entry("dpi").or_insert(serde_json::json!(dpi));
    o.entry("page").or_insert(serde_json::json!(page));
    fakescan::diag_v3::PageSpec::from_json(&v).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Ground-truth layout of the `diag` page as a JSON string (see `fakescan.diag`). Version 3 is the
/// parametric generator (`params`: JSON object of paper, orientation, margin_mm, ...); versions 1 and 2
/// are the fixed-size layouts.
#[pyfunction]
#[pyo3(signature = (dpi=100, version=3, params=None))]
fn diag_layout_json(dpi: u32, version: u32, params: Option<&str>) -> PyResult<String> {
    if version >= 3 {
        let spec = v3_spec(dpi, 0, params)?;
        return Ok(fakescan::diag_v3::build_layout(&spec).json().to_string());
    }
    Ok(fakescan::diag_v2::layout_json(dpi, version).to_string())
}

/// The `diag` page as `(width, height, RGB bytes)` (banner and QR included for v1 / v2).
#[pyfunction]
#[pyo3(signature = (dpi=100, page=0, version=3, params=None))]
fn diag_render_rgb(
    py: Python<'_>,
    dpi: u32,
    page: u32,
    version: u32,
    params: Option<&str>,
) -> PyResult<(u32, u32, Py<pyo3::types::PyBytes>)> {
    let img = if version >= 3 {
        fakescan::diag_v3::render(&v3_spec(dpi, page, params)?).0
    } else {
        fakescan::diag_v2::render(dpi, page, version).map_err(|e| PyRuntimeError::new_err(e.to_string()))?
    };
    let (w, h) = img.dimensions();
    Ok((w, h, pyo3::types::PyBytes::new(py, img.as_raw()).unbind()))
}

/// The `diag` page as PNG bytes with the sRGB colour space declared (v3 also carries the physical size).
#[pyfunction]
#[pyo3(signature = (dpi=100, page=0, version=3, params=None))]
fn diag_render_png(
    py: Python<'_>,
    dpi: u32,
    page: u32,
    version: u32,
    params: Option<&str>,
) -> PyResult<Py<pyo3::types::PyBytes>> {
    let png = if version >= 3 {
        let spec = v3_spec(dpi, page, params)?;
        let img = fakescan::diag_v3::render(&spec).0;
        fakescan::diag_v3::png_bytes(&img, spec.dpi)
    } else {
        let img = fakescan::diag_v2::render(dpi, page, version).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        fakescan::diag_v2::png_bytes(&img)
    }
    .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
    Ok(pyo3::types::PyBytes::new(py, &png).unbind())
}

/// Regenerate the page spec (JSON) from a v3 QR payload, or `None` when it is not one.
#[pyfunction]
fn diag_spec_from_qr(payload: &str) -> Option<String> {
    fakescan::diag_v3::PageSpec::from_qr_payload(payload).map(|s| s.json().to_string())
}

/// Paper names the generator knows (portrait sizes; custom `WxH` mm is always accepted).
#[pyfunction]
fn diag_paper_sizes() -> Vec<(String, f64, f64)> {
    fakescan::diag_v3::paper_names()
        .into_iter()
        .filter_map(|n| fakescan::diag_v3::paper_size_mm(n).map(|(w, h)| (n.to_string(), w, h)))
        .collect()
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<Server>()?;
    m.add_function(wrap_pyfunction!(diag_layout_json, m)?)?;
    m.add_function(wrap_pyfunction!(diag_render_rgb, m)?)?;
    m.add_function(wrap_pyfunction!(diag_render_png, m)?)?;
    m.add_function(wrap_pyfunction!(diag_spec_from_qr, m)?)?;
    m.add_function(wrap_pyfunction!(diag_paper_sizes, m)?)?;
    Ok(())
}
