#!/bin/bash
cd /d/gitScan
export AI_API_KEY="sk-40896e3a03a94c2c9f5a4c749a43b174"
export AI_BASE_URL="https://api.deepseek.com/v1"
export AI_MODEL="deepseek-chat"
exec .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
