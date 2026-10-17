import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import agent  # your agent.py from step 2
import messenger  # step 4

import poller

app = FastAPI(title="VelVet AlGo")
app.include_router(messenger.router)  # adds GET/POST /webhook
poller.start()

class ChatIn(BaseModel):
    customer_id: str   # later: the Messenger sender id
    message: str       # what the customer wrote


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/chat")
def chat(body: ChatIn):
    # a public /chat lets ANYONE act as any customer (cancel orders...). Only enable it while testing: DEBUG_CHAT=1
    if os.getenv("DEBUG_CHAT") != "1":
        raise HTTPException(status_code=404)
    reply = agent.reply_to(body.customer_id, body.message)
    return {"customer_id": body.customer_id, "reply": reply}

