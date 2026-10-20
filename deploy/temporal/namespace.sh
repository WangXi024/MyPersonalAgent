#!/bin/sh
set -eu
if temporal operator namespace describe --namespace learning-agent >/dev/null 2>&1; then
    echo 'Temporal namespace already exists.'
else
    temporal operator namespace create --namespace learning-agent --retention 7d
fi
