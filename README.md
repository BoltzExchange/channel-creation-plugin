# Boltz Channel Creation Plugin

This is a c-lightning plugin for Boltz Channel Creation Swaps

## Installation

This plugin is written in Python an works with Python version `3.7` or higher. Its dependencies can be installed with:

```bash
pip3 install pyln-client ecdsa requests
```

Or:

```bash
pip3 install -r requirements.txt
```

To install the plugin itself, download the Python script and make it executable:

```bash
wget https://raw.githubusercontent.com/BoltzExchange/channel-creation-plugin/master/channel-creation.py
chmod +x channel-creation.py
```

And finally run c-lightning with the plugin:

```bash
lightningd --plugin ~/path/to/channel-creation.py
```

If the plugin started correctly, you should see this message in the logs of c-lightning:

```
INFO plugin-channel-creation.py: Started channel-creation plugin
```

## Usage

When starting c-lightning, you have to provide the Boltz API endpoint and node public key of the Boltz Lightning node on your network with this argument:

```
--boltz-api <arg> --boltz-node <arg>
```

The Boltz API endpoint is:
- `https://boltz.exchange/api` for mainnet
- `https://testnet.boltz.exchange/api` for testnet

To find the right node public key, checkout the FAQ section of [boltz.exchange](https://boltz.exchange/faq) or [testnet.boltz.exchange](https://testnet.boltz.exchange/faq) or query your preferred Lightning network explorer for `Boltz`.

The plugin exposes the RPC commands:

```
addchannelcreation invoice_amount inbound_percentage [private]
    Adds a new Boltz Channel Creation Swap

getchannelcreation 
    Gets all available information about the added Boltz Channel Creation Swap
```

`add-channel-creation` tells the plugin to create an invoice for a Channel Creation Swap. `invoice_amount` is the amount of the invoice to be created in **millisatoshis**, `inbound_percentage` is the desired percentage of inbound liquidity Boltz should provide in the channel and `private` tells the plugin whether the channel Boltz opens should be private (default is `false`).

`get-channel-creation` allows you to query the status of the Channel Creation Swap.