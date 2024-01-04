import json
import logging
import time
from logging.handlers import SysLogHandler
from prometheus_client import start_http_server, Counter
import random
import ssl
import subprocess
from jsonschema import validate
import jsonschema
import pika
import config as cfg


SUCCESSFUL_COA = Counter('sucessful_coa_requests', "Number of sucessful Radius CoA Requests sent to switches")
FAILED_COA = Counter('failed_coa_requests', "Number of failed Radius CoA Requests sent to switches")
FAILED_MESSAGE_VALIDATE = Counter('failed_json_validate', "Number of failed messages which did not validate against the supplied schema")
FAILED_MESSAGE_DECODE = Counter('failed_json_decode', "Number of failed messages which were not valid json at all")
RABBIT_RECONNECTS = Counter('coa_rabbitmq_reconnect', "Number of reconnects to RabbitMQ servers")


def parse_message(message):
    try:
        logging.debug("received message from rabbitmq: %s" % message)
        # try to read body from queue as json
        message = json.loads(message)
        
        # open json schema for coa message
        f = open(cfg.JSON_SCHEMA_PATH)
        schema = json.load(f)

        # validate body against schema
        validate(message, schema)

        result = dict()
        result["NAS-Identifier"] = message["nasAddress"]
        result["NAS-Port-Id"] = message["nasPortId"]
        result["Vendor-Specific"] = "subscriber:command=" + message["coaCommand"]
        result["Calling-Station-Id"] = ".".join(message["macAddress"].replace(":", "")[i:i+4] for i in range(0, 12, 4))

        return result
    except json.JSONDecodeError:
        FAILED_MESSAGE_DECODE.inc()
        logging.error("could not parse json from message queue")
        return None
    except jsonschema.exceptions.ValidationError:
        FAILED_MESSAGE_VALIDATE.inc()
        logging.error("message received from message queue does not fit supplied schema")
        return None
    except jsonschema.exceptions.SchemaError:
        logging.error("could not load schema")
        return None


def send_coa(ch, method_frame, header_frame, body):
    attributes = parse_message(body)
    if not attributes:
        # abort if parsing failed
        return

    logging.debug("Attributes for CoA request: %s" % attributes)
    # create command to do coa reauth
    command = "echo \"Calling-Station-Id='%s', NAS-Port-Id='%s', Cisco-AVPair='%s'\" | radclient -r 1 %s:%s coa %s" % (attributes["Calling-Station-Id"], attributes["NAS-Port-Id"], attributes["Vendor-Specific"], attributes["NAS-Identifier"], cfg.RADIUS_PORT, cfg.RADIUS_SECRET),
    res = subprocess.run(command, shell=True, capture_output=True, check=False)
    if res.stderr:
        # inc error count if reauth was not successful
        # errors happen if the provided Calling-Station-Id does not match or the port is down
        logging.error("Got error reauthing port %s on switch %s. Error: %s" % (attributes["NAS-Port-Id"], attributes["NAS-Identifier"], res.stderr))
        FAILED_COA.inc()
    else:
        SUCCESSFUL_COA.inc()
 

def main():
    logger = logging.getLogger()
    logger.addHandler(SysLogHandler(address='/dev/log'))
    # import sys
    # logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.WARNING)

    # start prometheus http endpoint
    start_http_server(9765)

    while True:
        try:
            logging.debug("Connecting...")
            # Shuffle the hosts list before reconnecting.
            # This can help balance connections.
            random.shuffle(cfg.RABBITMQ_SERVER)
            logging.info(f"Connecting to server {cfg.RABBITMQ_SERVER[0]}")
            con = pika.BlockingConnection(pika.ConnectionParameters(host=cfg.RABBITMQ_SERVER[0],
                                                                    port=cfg.RABBITMQ_PORT,
                                                                    virtual_host=cfg.RABBITMQ_VHOST,
                                                                    ssl_options=pika.SSLOptions(context=ssl.create_default_context()),
                                                                    credentials=pika.PlainCredentials(cfg.RABBITMQ_USERNAME, cfg.RABBITMQ_PASSWORD)))
            ch = con.channel()
            if cfg.RABBITMQ_CREATE_ITEMS:
                # declare exchange if not existing yet
                ch.exchange_declare(cfg.RABBITMQ_EXCHANGE_NAME, exchange_type='direct')
                # create new queue only usable by this connection
                res = ch.queue_declare('', exclusive=True)
                queue_name = res.method.queue
                # bind queue to exchange
                ch.queue_bind(exchange=cfg.RABBITMQ_EXCHANGE_NAME, queue=queue_name)
                # consume from tmp queue
                ch.basic_consume(queue=queue_name, on_message_callback=send_coa, auto_ack=True)

            else:
                # consume from configured queue
                ch.basic_consume(queue=cfg.RABBITMQ_QUEUE_NAME, on_message_callback=send_coa, auto_ack=True)

            logging.debug("bound to rabbitmq channel")

            try:
                ch.start_consuming()
            except (KeyboardInterrupt, SystemExit):
                ch.stop_consuming()
                ch.close()
                break

        except pika.exceptions.ConnectionClosedByBroker:
            logging.warning("AQMP connection was closed by a broker, retrying...")
            RABBIT_RECONNECTS.inc()
            time.sleep(3)
            continue
        # Do not recover on channel errors
        except pika.exceptions.AMQPChannelError as err:
            logging.error("Caught a channel error: {}, stopping...".format(err))
            break
        # Recover on all other connection errors
        except pika.exceptions.AMQPConnectionError:
            RABBIT_RECONNECTS.inc()
            logging.warning("AMQP Connection was closed, retrying...")
            time.sleep(3)
            continue


if __name__ == "__main__":
    main()
