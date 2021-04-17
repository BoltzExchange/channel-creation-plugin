#!/usr/bin/env python3

# TODO: consistent error handling

import json
import math
import random
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from os import path, urandom
from typing import Mapping

import requests
from bitcoin import SelectParams
from bitcoin.core import CTransaction, CMutableTransaction, CMutableTxIn, CMutableTxOut, COutPoint, CTxWitness, \
    CTxInWitness, CScriptWitness
from bitcoin.core import x, b2x
from bitcoin.core.script import OP_0, OP_HASH160, OP_EQUAL, CScript, Hash160, SignatureHash, SIGHASH_ALL, \
    SIGVERSION_WITNESS_V0
from bitcoin.wallet import CBitcoinAddress, CBitcoinAddressError, CBitcoinSecret
from pyln.client import Plugin, Millisatoshi


class Status(Enum):
    Created = "created"
    ChannelAccepted = "channel_accepted"
    InvoicePaid = "invoice_paid"
    Refunded = "refunded"


@dataclass
class ChannelCreation:
    status: Status
    id: str

    private_key: str
    redeem_script: str

    private: bool
    invoice_amount: int
    inbound_percentage: int

    invoice_label: str
    preimage_hash: str

    address: str
    expected_amount: int
    bip21: str


PLUGIN = Plugin()


class BoltzApi:
    def __init__(self, api: str):
        self.api = api

    def get_nodes(self):
        request = requests.get("{}/getnodes".format(self.api))
        return request.json()

    def get_swap_transaction(self, swap_id: str):
        request = requests.post(
            "{}/getswaptransaction".format(self.api),
            json={
                "id": swap_id,
            },
        )
        return request.json()

    def create_swap(self, invoice: str, refund_key: str, private: bool, inbound: int):
        request = requests.post(
            "{}/createswap".format(self.api),
            json={
                "type": "submarine",
                "pairId": "BTC/BTC",
                "orderSide": "buy",
                "invoice": invoice,
                "refundPublicKey": refund_key,
                "channel": {
                    "auto": False,
                    "private": private,
                    "inboundLiquidity": inbound,
                },
            },
        )
        return request.json()


def print_error(message: str):
    print(message)
    return {
        "error": message[0].lower() + message[1:],
    }


def get_keys():
    entropy = urandom(32)
    private_key = CBitcoinSecret.from_secret_bytes(entropy)

    return entropy.hex(), private_key.pub.hex()


def format_channel_creation(channel_creation: ChannelCreation):
    return {
        "status": channel_creation.status.name,
        "id": channel_creation.id,
        "private_key": channel_creation.private_key,
        "redeem_script": channel_creation.redeem_script,
        "private": channel_creation.private,
        "invoice_amount": channel_creation.invoice_amount,
        "inbound_percentage": channel_creation.inbound_percentage,
        "invoice_label": channel_creation.invoice_label,
        "preimage_hash": channel_creation.preimage_hash,
        "address": channel_creation.address,
        "expected_amount": channel_creation.expected_amount,
        "bip21": channel_creation.bip21,
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
                id=raw_data["id"],
                private_key=raw_data["private_key"],
                redeem_script=raw_data["redeem_script"],
                private=raw_data["private"],
                invoice_amount=raw_data["invoice_amount"],
                inbound_percentage=raw_data["inbound_percentage"],
                invoice_label=raw_data["invoice_label"],
                preimage_hash=raw_data["preimage_hash"],
                address=raw_data["address"],
                expected_amount=raw_data["expected_amount"],
                bip21=raw_data["bip21"],
            )
            print("Read exiting channel creation state: {}".format(format_channel_creation(plugin.channel_creation)))

    except FileNotFoundError:
        print("Did not find existing channel creation state")


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


def find_swap_output(lockup_transaction: CTransaction, redeem_script: str) -> int:
    redeem_hash = sha256(x(redeem_script)).digest()

    pw2sh_script = CScript([OP_0, redeem_hash])
    p2sh_script = CScript([OP_HASH160, Hash160(pw2sh_script), OP_EQUAL]).hex()

    for i in range(len(lockup_transaction.vout)):
        if lockup_transaction.vout[i].scriptPubKey.hex() == p2sh_script:
            return i

    raise ValueError("could not find swap output")


def construct_refund_transaction(
        channel_creation: ChannelCreation,
        lockup_transaction: CTransaction,
        address: str,
        timeout_block_height: int,
) -> CMutableTransaction:
    redeem_script = CScript.fromhex(channel_creation.redeem_script)
    lockup_vout = find_swap_output(lockup_transaction, channel_creation.redeem_script)

    nested_script = b'\x22\x00\x20' + sha256(redeem_script).digest()
    inputs = [CMutableTxIn(COutPoint(lockup_transaction.GetTxid(), lockup_vout), nested_script, 0xfffffffd)]

    try:
        output_script = CBitcoinAddress(address).to_scriptPubKey()
    except CBitcoinAddressError:
        raise ValueError("Could not parse Bitcoin address: {}".format(address))

    input_amount = lockup_transaction.vout[lockup_vout].nValue

    # TODO: configurable transaction fee + reasonable default
    output_amount = input_amount - 1000

    outputs = [CMutableTxOut(output_amount, output_script)]

    refund_transaction = CMutableTransaction(inputs, outputs, timeout_block_height)

    sighash = SignatureHash(
        inIdx=0,
        amount=input_amount,
        hashtype=SIGHASH_ALL,
        script=redeem_script,
        txTo=refund_transaction,
        sigversion=SIGVERSION_WITNESS_V0,
    )

    key = CBitcoinSecret.from_secret_bytes(bytes.fromhex(channel_creation.private_key))
    sig = key.sign(sighash) + bytes([SIGHASH_ALL])

    refund_transaction.wit = CTxWitness([CTxInWitness(CScriptWitness([sig, bytes(), redeem_script]))])

    return refund_transaction


@PLUGIN.init()
def init(plugin: Plugin, options: Mapping[str, str], **_kwargs):
    plugin.data_location = path.join(plugin.rpc.listconfigs()["lightning-dir"], "channel-creation.json")

    print("Channel Creation data location: {}".format(plugin.data_location))
    read_channel_creation(plugin)

    info = plugin.rpc.getinfo()
    network = info["network"]

    SelectParams(network)

    if options["boltz-api"] == "":
        if network == "mainnet":
            api_url = "https://boltz.exchange/api"
        elif network == "testnet":
            api_url = "https://testnet.boltz.exchange/api"
        elif network == "regtest":
            api_url = "http://127.0.0.1:9001"
        else:
            raise ValueError("No default API for network {} available".format(network))

        plugin.boltz_api = BoltzApi(api_url)
        print("Using default Boltz API for network {}: {}".format(network, api_url))

    else:
        plugin.boltz_api = BoltzApi(options["boltz-api"])

    plugin.boltz_node = options["boltz-node"]
    if plugin.boltz_node == "":
        nodes = plugin.boltz_api.get_nodes()
        plugin.boltz_node = nodes["nodes"]["BTC"]["nodeKey"]

        print("Fetched Boltz lightning node public key: {}".format(plugin.boltz_node))

    print("Started channel-creation plugin: {}".format({
        "boltz_api": plugin.boltz_api.api,
        "boltz_node": plugin.boltz_node,
    }))


#
# RPC methods
#
# TODO: create valid refund.json
# TODO: sanity check if there is a channel already
@PLUGIN.method("addchannelcreation")
def add_channel_creation(plugin: Plugin, invoice_amount: int, inbound_percentage: int, private=False):
    """Adds a new Boltz Channel Creation Swap"""
    if hasattr(plugin, "channel_creation") \
            and plugin.channel_creation is not None \
            and (plugin.channel_creation.status is not Status.InvoicePaid and
                 plugin.channel_creation.status is not Status.Refunded):
        return {
            "error": "there is a pending channel creation already",
        }

    peers = plugin.rpc.listpeers()["peers"]

    for peer in peers:
        if peer["id"] == plugin.boltz_node and len(peer["channels"]) != 0:
            channels = peer["channels"]

            for channel in channels:
                if channel["state"] != "ONCHAIN":
                    return {
                        "error": "there is a channel with the Boltz node already"
                    }

    try:
        invoice_label = "boltz-channel-{}".format(random.randint(0, 100000))
        invoice_response = plugin.rpc.invoice(
            invoice_amount,
            invoice_label,
            "Boltz Channel Creation Swap",
        )

        private_key, public_key = get_keys()
        swap = plugin.boltz_api.create_swap(
            invoice_response["bolt11"],
            public_key,
            private,
            inbound_percentage,
        )

        if "error" in swap:
            return print_error("Could not setup channel creation: {}".format(str(swap["error"])))

        print("Created swap: {}".format(swap))

        plugin.channel_creation = ChannelCreation(
            id=swap["id"],
            private=private,
            bip21=swap["bip21"],
            status=Status.Created,
            address=swap["address"],
            private_key=private_key,
            invoice_label=invoice_label,
            invoice_amount=invoice_amount,
            redeem_script=swap["redeemScript"],
            inbound_percentage=inbound_percentage,
            expected_amount=swap["expectedAmount"],
            preimage_hash=invoice_response["payment_hash"],
        )
        write_channel_creation(plugin, plugin.channel_creation)
        print("Added channel creation: {}".format(format_channel_creation(plugin.channel_creation)))

        return {
            "address": plugin.channel_creation.address,
            "expectedAmount": plugin.channel_creation.expected_amount,
            "bip21": swap["bip21"],
        }
    except requests.ConnectionError or requests.HTTPError as error:
        return print_error("Could not add channel creation: {}".format(str(error)))


@PLUGIN.method("getchannelcreation")
def get_channel_creation(plugin: Plugin):
    """Gets all available information about the added Boltz Channel Creation Swap"""
    if not hasattr(plugin, "channel_creation"):
        return {
            "error": "no channel creation was added"
        }

    return format_channel_creation(plugin.channel_creation)


@PLUGIN.method("refundchannelcreation")
def refund_channel_creation(plugin: Plugin, address=""):
    """Refunds the lockup transaction of a Channel Creation Swap"""
    if not hasattr(plugin, "channel_creation") or \
            plugin.channel_creation is None or \
            (plugin.channel_creation.status is Status.InvoicePaid or plugin.channel_creation.status is Status.Refunded):
        return {
            "error": "no refundable channel creation found"
        }

    print("Refunding channel creation {}".format(plugin.channel_creation.id))

    info = plugin.rpc.getchaininfo()
    swap_transaction = plugin.boltz_api.get_swap_transaction(plugin.channel_creation.id)

    if hasattr(swap_transaction, "error"):
        return {
            "error": "could not find lockup transaction: {}".format(swap_transaction["error"]),
        }

    if info["blockcount"] < swap_transaction["timeoutBlockHeight"]:
        return {
            "error": "channel creation cannot be refunded before block height {}"
            .format(swap_transaction["timeoutBlockHeight"])
        }

    if address == "":
        new_addr = plugin.rpc.newaddr()["address"]
        address = new_addr
        print("Got address to refund to: {}".format(address))

    lock_transaction = CTransaction.deserialize(x(swap_transaction["transactionHex"]))

    refund_transaction = construct_refund_transaction(
        plugin.channel_creation,
        lock_transaction,
        address,
        swap_transaction["timeoutBlockHeight"],
    )

    refund_transaction_hex = b2x(refund_transaction.serialize())
    refund_transaction_id = b2x(refund_transaction.GetTxid()[::-1])

    print("Constructed refund transaction {}: {}".format(refund_transaction_id, refund_transaction_hex))

    broadcast_response = plugin.rpc.sendrawtransaction(refund_transaction_hex, False)

    if broadcast_response["success"]:
        print("Broadcast refund transaction: {}".format(refund_transaction_id))
        update_channel_creation_status(plugin, plugin.channel_creation, Status.Refunded)
    else:
        return {
            "error": "could not broadcast refund transaction {}"
            .format(broadcast_response["errmsg"])
        }

    return {
        "txid": refund_transaction_id,
        "txhex": refund_transaction_hex,
    }


#
# Hooks
#
@PLUGIN.hook("openchannel")
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


@PLUGIN.async_hook("invoice_payment")
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


# TODO: automatically connect to node
PLUGIN.add_option(
    "boltz-api",
    "",
    "Boltz API endpoint"
)

PLUGIN.add_option(
    "boltz-node",
    "",
    "Public key of the Boltz Lightning node"
)

PLUGIN.run()
