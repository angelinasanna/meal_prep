#!/bin/bash
set -e

if [ ! -f ".env" ]; then
  echo "⚠️  No .env file found. Creating one from .env.example..."
  cp .env.example .env
  echo ""
  echo "👉 Edit .env and add your API keys (see the comments inside), then run this script again."
  exit 1
fi

echo "🌿 Starting mealprepped..."
uvicorn main:app --reload --port 8000
