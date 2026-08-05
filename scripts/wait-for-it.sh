#!/bin/bash
# Wait for a TCP host:port to be available
HOST="$1"
PORT="$2"
shift 2
until nc -z "$HOST" "$PORT" 2>/dev/null; do
  echo "Waiting for $HOST:$PORT..."
  sleep 2
done
exec "$@"
