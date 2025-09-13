#!/bin/bash

# Kill the current program if running
killall python3

# Upload the new version
scp -r ./src/* root@192.168.1.10:/opt/rc-car/web-server

# Run the program on the target
ssh root@192.168.1.10 "python3 /opt/rc-car/web-server &"

echo "Upload and restart complete!"
