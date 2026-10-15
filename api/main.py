import json
import logging
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictBool

from agent.console import force_utf8_console
from agent.locking import exclusive_lock, OperationBusyError
from agent.milvus_data_sync import run_menu_sync
from agent.order_agent import chat_with_agent, stream_chat_with_agent
from agent import session_store
from agent.reservations import submit_reservation

logger = logging.getLogger(__name__)
app = FastAPI(title="SmartOrderAgent API", version="1.2.0")
# Web 与 API 同源；不开放跨域凭证访问。


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    reservation_draft: dict | None = None


class ConfirmationRequest(BaseModel):
    confirm: StrictBool


def authenticated_session(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先创建会话")
    session_id = session_store.authenticate(authorization[7:])
    if not session_id:
        raise HTTPException(status_code=401, detail="会话凭证无效或已过期")
    return session_id


def require_admin(x_admin_token: str | None = Header(default=None)):
    expected = os.getenv("ADMIN_API_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="管理接口未配置")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="管理凭证无效")


def check_request(req, session_id):
    if req.session_id is not None and req.session_id != session_id:
        raise HTTPException(status_code=403, detail="无权访问此会话")
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="请求内容不能为空")
    return message


def event(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "SmartOrderAgent"}


@app.get("/")
def serve_index():
    return FileResponse(Path(__file__).resolve().parents[1] / "web" / "index.html")


@app.post("/sessions")
def create_session():
    return session_store.create_session()


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(req: ChatRequest, session_id=Depends(authenticated_session)):
    message = check_request(req, session_id)
    try:
        with exclusive_lock("chat:" + session_id):
            reply = chat_with_agent(user_input=message, thread_id=session_id)
            draft = session_store.pending_draft(session_id)
    except OperationBusyError:
        raise HTTPException(status_code=409, detail="此会话正在处理其他请求")
    except Exception:
        logger.exception("对话处理失败")
        raise HTTPException(status_code=500, detail="处理失败，请稍后重试")
    return ChatResponse(reply=reply, session_id=session_id, reservation_draft=draft)


@app.post("/chat/stream")
def chat_stream_endpoint(req: ChatRequest, session_id=Depends(authenticated_session)):
    message = check_request(req, session_id)
    def generate():
        try:
            with exclusive_lock("chat:" + session_id):
                for token in stream_chat_with_agent(user_input=message, thread_id=session_id):
                    yield event({"type": "token", "text": token})
                yield event({"type": "reservation", "draft": session_store.pending_draft(session_id)})
                yield event({"type": "done"})
        except OperationBusyError:
            yield event({"type": "error", "message": "此会话正在处理其他请求"})
        except Exception:
            logger.exception("流式对话失败")
            yield event({"type": "error", "message": "处理失败，请稍后重试"})
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/reservations/pending")
def pending(session_id=Depends(authenticated_session)):
    return {"draft": session_store.pending_draft(session_id)}


@app.post("/reservations/{draft_id}/confirm")
def confirm_reservation(draft_id: str, req: ConfirmationRequest, session_id=Depends(authenticated_session)):
    if req.confirm is not True:
        raise HTTPException(status_code=400, detail="必须明确确认预订明细")
    try:
        return submit_reservation(session_id, draft_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="预订草稿不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("确认预订失败")
        raise HTTPException(status_code=500, detail="提交失败，可重试同一草稿，不会重复下单")


@app.post("/admin/sync-menu", dependencies=[Depends(require_admin)])
def trigger_menu_sync():
    try:
        return run_menu_sync()
    except OperationBusyError:
        raise HTTPException(status_code=409, detail="菜单同步正在执行")
    except Exception:
        logger.exception("菜单向量库同步失败")
        raise HTTPException(status_code=500, detail="菜单同步失败，请查看服务端日志")


if __name__ == "__main__":
    import uvicorn
    force_utf8_console()
    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=True)
