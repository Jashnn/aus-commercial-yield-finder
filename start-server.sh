#!/bin/bash
# Wrapper called by the LaunchAgent. Uses the same python3 as the user's shell.
cd "$(dirname "$0")"
exec /usr/bin/python3 server.py
