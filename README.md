# CoA Worker

The scripts in this repo are used to send Radius Change of Authorization (CoA) requests to network devices (NAS) in order 
to reauthenticate specific ports. The source for the reauthentication requests is a RabbitMQ queue.

## CoA

Change of Authorization (CoA) is used to reauthenticate ports which were previously authenticated using 802.1X mechanisms
on a network device (NAS). This can be needed if an event occurs that makes it nessecery to reauthenticate a session
(user got invalidated, VLAN assginemtn changed, etc.).
This script uses the CoA request type ```bounce-host-port```. With this command, teh port is completely shut for a brief 
moment, before a new session can begin.

## Message format

Messages inside the queue should be in the format specified in [radius_coa.schema.json](radius_coa.schema.json). 

## Monitoring

The script exposes a Prometheus endpoint on port ```9765```. 