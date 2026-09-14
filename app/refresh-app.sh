#!/bin/bash

docker compose build --no-cache
#docker compose /opt/blpl/app/docker-compose.yml up -d
docker compose down
docker compose up -d
