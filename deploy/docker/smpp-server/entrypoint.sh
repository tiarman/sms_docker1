#!/bin/bash
set -e

echo "Waiting for PostgreSQL to become available at $DB_HOST:$DB_PORT..."
until nc -z $DB_HOST $DB_PORT; do
  sleep 1
done
echo "PostgreSQL is available."

echo "Waiting for Redis to become available at $REDIS_HOST:$REDIS_PORT..."
until nc -z $REDIS_HOST $REDIS_PORT; do
  sleep 1
done
echo "Redis is available."

echo "Waiting for RabbitMQ to become available at $RABBITMQ_HOST:$RABBITMQ_PORT..."
until nc -z $RABBITMQ_HOST $RABBITMQ_PORT; do
  sleep 1
done
echo "RabbitMQ is available."

echo "Running database migrations..."
python -c "from smpp_server.db import migrate; migrate()"

echo "Starting SMPP server..."
exec python -m smpp_server.main