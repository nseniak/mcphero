#!/bin/bash

set -e

eval $(ssh-agent -s)
ssh-add ~/.ssh/github

git pull
docker compose --profile cloud up --build -d
