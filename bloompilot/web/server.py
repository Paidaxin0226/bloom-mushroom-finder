from __future__ import annotations

import argparse
import asyncio
import os
import signal
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import __version__
from ..diagnostics import configure_diagnostics
from ..paths import PROJECT_ROOT, TRAINING_CONTROL_DIR
from ..training_control import TrainingControl
from .runtime import AppRuntime, JobRecord


class DeviceConnectRequest(BaseModel):
    serial: str = Field(min_length=1, max_length=200)


class DeviceCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)


class ViewportCalibrationRequest(BaseModel):
    serial: str = Field(min_length=1, max_length=200)
    action: Literal["capture", "step", "recenter"]
    direction: Literal["west", "east", "north", "south"] | None = None
    settle_seconds: float = Field(default=2.0, ge=0, le=30)
    swipe_duration_ms: int = Field(default=800, ge=100, le=5000)


class DeviceScaleCalibrationRequest(BaseModel):
    serial: str = Field(min_length=1, max_length=200)
    settle_seconds: float = Field(default=2.5, ge=0.5, le=10)


class RouteImportRequest(BaseModel):
    source_name: str = Field(min_length=1, max_length=260)
    data: Any


class HitVisitRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: str = Field(default="", max_length=200)


class RecognitionFilterRequest(BaseModel):
    target_names: list[str] | None = None
    participant_count: int | None = Field(default=None, ge=0)


class SkipWaypointRequest(BaseModel):
    waypoint_id: str = Field(min_length=1, max_length=100)


class DefaultModelRequest(BaseModel):
    model_path: str = Field(min_length=1, max_length=1000)


class DiscordWebhookRequest(BaseModel):
    mode: Literal["webhook", "bot"] = "webhook"
    webhook_url: str = Field(default="", max_length=2048, repr=False)
    bot_token: str = Field(default="", max_length=300, repr=False)
    channel_id: str = Field(default="", max_length=30)


class DiscordBotRequest(BaseModel):
    bot_token: str = Field(min_length=1, max_length=300, repr=False)


def job_payload(record: JobRecord) -> dict[str, Any]:
    return {"job": record.to_dict()}


def create_app(runtime: AppRuntime | None = None) -> FastAPI:
    app_runtime = runtime or AppRuntime()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        app_runtime.hub.bind(asyncio.get_running_loop())
        app_runtime.hub.publish({"type": "system", "state": "ready"})
        yield
        app_runtime.close()

    app = FastAPI(
        title="BloomPilot Desktop API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.runtime = app_runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "null",
        ],
        allow_origin_regex=r"^http://(?:localhost|127\.0\.0\.1)(?::[0-9]+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "name": "BloomPilot",
            "version": __version__,
            "project_root": str(PROJECT_ROOT),
        }

    @app.get("/api/device/list")
    def list_devices() -> list[dict[str, Any]]:
        try:
            return app_runtime.device.scan()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/device/connect")
    def connect_device(request: DeviceConnectRequest) -> dict[str, Any]:
        try:
            return app_runtime.connect_device(request.serial)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/device/connect")
    def disconnect_device() -> dict[str, Any]:
        active = app_runtime.jobs.active()
        if active is not None and active.kind == "geo_sequence_recognition":
            raise HTTPException(409, "座標序列辨識正在使用裝置控制，請先停止工作。")
        return app_runtime.disconnect_device()

    @app.get("/api/device/status")
    def device_status() -> dict[str, Any]:
        try:
            return app_runtime.device_status()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/device/command")
    def device_command(request: DeviceCommandRequest) -> dict[str, Any]:
        try:
            return app_runtime.device.command(request.command, request.payload)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/device/screen-control")
    def screen_control_status() -> dict[str, object]:
        return app_runtime.screen_control.status()

    @app.get("/api/device/phone-state")
    def phone_state(serial: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
        try:
            return app_runtime.phone_state(serial)
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/device/wake")
    def wake_phone(request: DeviceConnectRequest) -> dict[str, Any]:
        try:
            return app_runtime.wake_phone(request.serial)
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/device/screen-control")
    def open_screen_control() -> dict[str, object]:
        try:
            return app_runtime.open_screen_control()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.delete("/api/device/screen-control")
    def close_screen_control() -> dict[str, object]:
        return app_runtime.close_screen_control()

    @app.post("/api/device/viewport-calibration")
    def viewport_calibration(request: ViewportCalibrationRequest) -> dict[str, Any]:
        try:
            return app_runtime.calibrate_viewport(
                serial=request.serial,
                action=request.action,
                direction=request.direction,
                settle_seconds=request.settle_seconds,
                swipe_duration_ms=request.swipe_duration_ms,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/device/scale-calibration")
    def get_device_scale_calibration(serial: str) -> dict[str, Any]:
        return app_runtime.device_scale(serial)

    @app.post("/api/device/scale-calibration")
    def calibrate_device_scale(request: DeviceScaleCalibrationRequest) -> dict[str, Any]:
        try:
            return app_runtime.calibrate_device_scale(
                serial=request.serial,
                settle_seconds=request.settle_seconds,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return app_runtime.settings_payload()

    @app.put("/api/settings")
    def put_settings(settings: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return app_runtime.save_settings(settings)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/settings")
    def patch_settings(settings: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return app_runtime.patch_settings(settings)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/notifications/discord/test")
    def test_discord_webhook(request: DiscordWebhookRequest) -> dict[str, Any]:
        try:
            if request.mode == "bot":
                return app_runtime.test_discord_bot(request.bot_token, request.channel_id)
            return app_runtime.test_discord_webhook(request.webhook_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/notifications/discord/channels")
    def discord_bot_channels(request: DiscordBotRequest) -> dict[str, Any]:
        try:
            return app_runtime.discord_bot_channels(request.bot_token)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/models")
    def get_models() -> list[dict[str, Any]]:
        return app_runtime.models()

    @app.put("/api/models/default")
    def set_default_model(request: DefaultModelRequest) -> dict[str, Any]:
        try:
            return {"model": app_runtime.set_default_recognition_model(request.model_path)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/datasets")
    def get_datasets() -> list[dict[str, Any]]:
        return app_runtime.datasets()

    @app.get("/api/routes")
    def get_routes() -> list[dict[str, Any]]:
        return app_runtime.routes()

    @app.get("/api/monthly-activity/status")
    def monthly_activity_status(cycle: str) -> dict[str, Any]:
        try:
            return app_runtime.monthly_status(cycle)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/monthly-activity/config")
    def configure_monthly_activity(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return app_runtime.configure_monthly_workflow(payload)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/monthly-activity/target")
    def save_monthly_activity_target(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return app_runtime.save_monthly_activity_target(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/monthly-activity/background")
    def get_monthly_activity_background(cycle: str, path: str, thumbnail: bool = False) -> Response:
        try:
            if thumbnail:
                return Response(app_runtime.monthly_background_thumbnail(cycle, path), media_type="image/jpeg")
            return FileResponse(app_runtime.monthly_background_file(cycle, path))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/monthly-activity/background")
    def delete_monthly_activity_background(cycle: str, path: str) -> dict[str, Any]:
        try:
            return app_runtime.delete_monthly_background(cycle, path)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/monthly-activity/delete-backgrounds")
    def delete_monthly_activity_backgrounds(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return app_runtime.delete_monthly_backgrounds(str(payload.get("cycle") or ""), payload.get("paths"))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/monthly-activity/approve-backgrounds")
    def approve_monthly_activity_backgrounds(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return app_runtime.approve_monthly_backgrounds(str(payload.get("cycle") or ""))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/routes/content")
    def get_route_content(path: str) -> FileResponse:
        try:
            return FileResponse(app_runtime.route_path(path), media_type="application/json")
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/routes/import")
    def import_route(request: RouteImportRequest) -> dict[str, Any]:
        try:
            return {"route": app_runtime.import_route(request.source_name, request.data)}
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"無法寫入專案 routes 目錄：{exc}") from exc

    @app.delete("/api/routes")
    def delete_route(path: str) -> dict[str, Any]:
        try:
            return {"route": app_runtime.delete_route(path)}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"無法刪除專案 routes 序列：{exc}") from exc

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        active = app_runtime.jobs.active()
        return {
            "active": active.to_dict() if active else None,
            "jobs": app_runtime.jobs.list(),
        }

    @app.post("/api/system/shutdown")
    async def shutdown() -> dict[str, bool]:
        app_runtime.close()
        loop = asyncio.get_running_loop()
        request_shutdown = getattr(app.state, "request_shutdown", None)
        if callable(request_shutdown):
            loop.call_later(0.2, request_shutdown)
        else:
            loop.call_later(0.2, lambda: os.kill(os.getpid(), signal.SIGINT))
        return {"ok": True}

    @app.post("/api/jobs/{kind}")
    def start_job(kind: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        starters = {
            "dataset": app_runtime.start_dataset,
            "training": app_runtime.start_training,
            "background_collection": app_runtime.start_background_collection,
            "monthly_background_collection": app_runtime.start_monthly_background_collection,
            "monthly_training": app_runtime.start_monthly_training,
            "screen_recognition": app_runtime.start_recognition,
            "geo_sequence_recognition": app_runtime.start_geo_sequence_recognition,
            "live_recognition": app_runtime.start_live_recognition,
        }
        starter = starters.get(kind)
        if starter is None:
            raise HTTPException(404, f"不支援的工作類型：{kind}")
        try:
            return job_payload(starter(payload))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.stop_job(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/skip-waypoint")
    def skip_waypoint(job_id: str, request: SkipWaypointRequest) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.jobs.skip_geo_waypoint(job_id, request.waypoint_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/delete-waypoint")
    def delete_waypoint(job_id: str, request: SkipWaypointRequest) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.delete_geo_waypoint(job_id, request.waypoint_id))
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"無法從 route 刪除節點，尚未跳過目前節點：{exc}") from exc

    @app.post("/api/jobs/{job_id}/visit-hit")
    def visit_hit(job_id: str, request: HitVisitRequest) -> dict[str, Any]:
        try:
            return job_payload(
                app_runtime.teleport_while_paused(
                    job_id,
                    lat=request.lat,
                    lng=request.lng,
                    label=request.label,
                )
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/pause")
    def pause_recognition(job_id: str) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.pause_recognition(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/pause-request")
    def request_recognition_pause(job_id: str) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.request_recognition_pause(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/cancel-pause")
    def cancel_recognition_pause(job_id: str) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.cancel_pending_recognition_pause(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/resume")
    def resume_recognition(
        job_id: str,
        request: RecognitionFilterRequest = Body(default_factory=RecognitionFilterRequest),
    ) -> dict[str, Any]:
        try:
            return job_payload(
                app_runtime.resume_recognition(job_id, request.model_dump())
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/return-from-hit")
    def return_from_hit(job_id: str) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.resume_recognition(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/training/pause")
    def pause_training() -> dict[str, Any]:
        active = app_runtime.jobs.active()
        if active is None or active.kind != "training":
            raise HTTPException(409, "目前沒有執行中的訓練工作。")
        try:
            return app_runtime.pause_training()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/cancel-stop")
    def cancel_training_stop(job_id: str) -> dict[str, Any]:
        try:
            return job_payload(app_runtime.cancel_training_stop(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/training/status")
    def training_status() -> dict[str, Any]:
        return TrainingControl(TRAINING_CONTROL_DIR).read_status()

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        event_queue = app_runtime.hub.subscribe()
        try:
            await websocket.send_json({"type": "system", "state": "ready"})
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    event = {"type": "heartbeat"}
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            app_runtime.hub.unsubscribe(event_queue)

    return app


app = create_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BloomPilot Electron desktop API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18777)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("BloomPilot desktop API 只允許綁定 loopback。")
    configure_diagnostics()
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="info",
        access_log=False,
    ))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
