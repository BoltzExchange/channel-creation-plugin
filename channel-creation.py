#!/usr/bin/env python3
import math
import json
import random
from os import path
from enum import Enum
from typing import Mapping
from dataclasses import dataclass
from pyln.client import Plugin, Millisatoshi


class Status(Enum):
    Created = "created"
    ChannelAccepted = "channel_accepted"
    InvoicePaid = "invoice_paid"


@dataclass
class ChannelCreation:
    status: Status

    private: bool
    invoice_amount: int
    inbound_percentage: int

    invoice_label: str
    preimage_hash: str


plugin = Plugin()


def format_channel_creation(channel_creation: ChannelCreation):
    return {
        "status": channel_creation.status.name,
        "private": channel_creation.private,
        "invoice_amount": channel_creation.invoice_amount,
        "inbound_percentage": channel_creation.inbound_percentage,
        "invoice_label": channel_creation.invoice_label,
        "preimage_hash": channel_creation.preimage_hash,
    }


def write_channel_creation(plugin: Plugin, channel_creation: ChannelCreation):
    with open(plugin.data_location, 'w') as file:
        json.dump(format_channel_creation(channel_creation), file)


def update_channel_creation_status(plugin: Plugin, channel_creation: ChannelCreation, new_state: Status):
    channel_creation.status = new_state
    write_channel_creation(plugin, channel_creation)


def read_channel_creation(plugin: Plugin):
    try:
        with open(plugin.data_location, 'r') as file:
            raw_data = json.load(file)
            plugin.channel_creation = ChannelCreation(
                status=Status[raw_data["status"]],
                private=raw_data["private"],
                invoice_amount=raw_data["invoice_amount"],
                inbound_percentage=raw_data["inbound_percentage"],
                invoice_label=raw_data["invoice_label"],
                preimage_hash=raw_data["preimage_hash"],
            )
            print("Read exiting channel creation state: {}".format(format_channel_creation(plugin.channel_creation)))

    except FileNotFoundError:
        print("Did not file existing channel creation state")


def check_channel_open(openchannel, plugin: Plugin) -> str:
    push_amount = Millisatoshi(openchannel["push_msat"]).millisatoshis

    if push_amount != 0:
        return "push amount not 0"

    channel_creation = plugin.channel_creation
    capacity = Millisatoshi(openchannel["funding_satoshis"]).millisatoshis

    expected_capacity = math.floor(channel_creation.invoice_amount / (1 - (channel_creation.inbound_percentage / 100)))

    if expected_capacity > capacity:
        return "minimum capacity requirement of {}msat not met".format(expected_capacity)

    channel_flags = openchannel["channel_flags"]

    # A private channel has "channel_flags" 0
    if channel_creation.private and channel_flags == 1:
        return "channel is not private"
    elif not channel_creation.private and channel_flags == 0:
        return "channel is not public"

    return ""


@plugin.init()
def init(plugin: Plugin, options: Mapping[str, str], **_kwargs):
    plugin.boltz_node = options["boltz-node"]
    plugin.data_location = path.join(plugin.rpc.listconfigs()["lightning-dir"], "channel-creation.json")

    print("Channel Creation data location: {}".format(plugin.data_location))
    read_channel_creation(plugin)

    print("Started channel-creation plugin")


@plugin.method("add-channel-creation")
def add_channel_creation(plugin: Plugin, invoice_amount: int, inbound_percentage: int, private=False):
    """Adds a new Boltz Channel Creation Swap"""
    if hasattr(plugin, "channel_creation") and plugin.channel_creation.status is not Status.InvoicePaid:
        return {
            "error": "there is a pending channel creation already"
        }

    invoice_label = "boltz-channel-{}".format(random.randint(0, 100000))

    invoice_response = plugin.rpc.invoice(
        invoice_amount,
        invoice_label,
        "Boltz Channel Creation Swap",
    )

    plugin.channel_creation = ChannelCreation(
        private=private,
        status=Status.Created,
        invoice_amount=invoice_amount,
        inbound_percentage=inbound_percentage,
        invoice_label=invoice_label,
        preimage_hash=invoice_response["payment_hash"]
    )
    write_channel_creation(plugin, plugin.channel_creation)
    print("Added channel creation: {}".format(format_channel_creation(plugin.channel_creation)))

    return {
        "status": str(plugin.channel_creation.status),
        "invoice": invoice_response["bolt11"],
    }


@plugin.method("get-channel-creation")
def get_channel_creation(plugin: Plugin):
    """Gets all available information about the added Boltz Channel Creation Swap"""
    if not hasattr(plugin, "channel_creation"):
        return {
            "error": "no channel creation was added"
        }

    return format_channel_creation(plugin.channel_creation)


@plugin.hook("openchannel")
def on_openchannel(openchannel, plugin: Plugin, **_kwargs):
    if openchannel["id"] != plugin.boltz_node or \
            not hasattr(plugin, "channel_creation") or \
            plugin.channel_creation.status != Status.Created:
        return {
            "result": "continue",
        }

    error = check_channel_open(openchannel, plugin)

    if error != "":
        print("Rejected channel creation: {}".format(error))
        return {
            "result": "reject",
            "error_message": error,
        }

    update_channel_creation_status(plugin, plugin.channel_creation, Status.ChannelAccepted)
    print("Accepted channel creation")

    return {
        "result": "continue",
    }


@plugin.async_hook("invoice_payment")
def on_invoice_payment(payment, plugin: Plugin, request, **_kwargs):
    if not hasattr(plugin, "channel_creation") or plugin.channel_creation.invoice_label != payment["label"]:
        request.set_result({
            "result": "continue"
        })
        return

    # No channel has been opened yet
    if plugin.channel_creation.status != Status.ChannelAccepted:
        print("Rejected invoice payment: no channel was opened yet")
        request.set_result({
            "result": "reject",
        })
        return

    sent_over_right_channel = False

    channel = plugin.rpc.listpeers(plugin.boltz_node)["peers"][0]["channels"][-1]

    for htlc in channel["htlcs"]:
        if htlc["payment_hash"] == plugin.channel_creation.preimage_hash:
            sent_over_right_channel = True
            break

    if not sent_over_right_channel:
        print("Rejected invoice payment: it was not paid trough the right channel")
        request.set_result({
            "result": "reject",
        })
        return

    update_channel_creation_status(plugin, plugin.channel_creation, Status.InvoicePaid)
    print("Accepted invoice payment")

    request.set_result({
        "result": "continue"
    })


plugin.add_option(
    "boltz-node", "02b9ccd9498a4adc3add6105cb4f10dbe1e10a0a15a6c6c5f461e43af9b5d6d47a",
    "Public key of the Boltz Lightning node"
)

plugin.run()
