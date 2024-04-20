# Copyright (C) 2024
# This is an example of PatternSeries construction from the EtherNet/IP protocol.
# Contains the following content:

# Protocol field feature analysis, PatternSeries generation.



import json
import random
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from utils.flow_utils import preprocess_flow, generate_ts_datasets, get_value_from_partial_key, \
    code_method

with open("./datasets_preprocessing/datasets/swat/cip_flow_ot_0.01.json", encoding='utf-8') as f:
# with open("./datasets_preprocessing/datasets/ctf/cip_flow_ot_0.01.json", encoding='utf-8') as f:  # SWAT
    # with open("CIP.json", encoding='utf-8') as f: # CTF
    line = f.readline()
    messages = json.loads(line)
    f.close()

msgs = messages
# flow size
seglen = [len(msg["segments"]) for msg in msgs]
pktcnt = Counter(seglen)

newlow = preprocess_flow(msgs)
valflow = [nw for nw in newlow if len(nw) >= 100]

# Generate ts datasets
mysets = []
fiels = []
for nw in newlow:
    segs = sorted(nw, key=lambda x: x['size'])
    cmd = []
    length = []
    sessids = []
    magic = []  # protocol identifier 0x0800
    status = []
    count = []
    service = []
    addr = []
    sesihandle = []
    senderCtx = []
    fake = []
    fake2 = []
    timestamp = []
    offset = []
    for seg in segs:
        # "enip"
        magic.append("0x0800")
        cmd.append(seg["enip"]["Encapsulation Header"]["enip.command"])
        length.append(seg["enip"]["Encapsulation Header"]["enip.length"])
        # sessids.append(seg["enip"]["Command Specific Data"]["enip.cpf.itemcount_tree"]["enip.cpf.typeid_tree"]["cip.seq"])
        sessids.append(
            seg.get("enip").get("Command Specific Data").get("enip.cpf.itemcount_tree").get("enip.cpf.typeid_tree").get(
                "cip.seq", "0"))
        timestamp.append(seg.get("enip").get("Command Specific Data").get("enip.time", "0"))
        status.append(seg["enip"]["Encapsulation Header"]["enip.status"])
        count.append(seg["enip"]["Command Specific Data"]["enip.cpf.itemcount"])
        service.append(seg["cip"]["cip.service"])
        sesihandle.append(seg["enip"]["Encapsulation Header"]["enip.context"])
        fake.append(random.random())
        fake2.append(random.random())

        dt = seg.get("cip")
        app_layer = get_value_from_partial_key(dt, "Request Path")
        multSerPacket = dt.get(get_value_from_partial_key(dt, "Multiple Service Packet"))
        if app_layer is not None and multSerPacket is not None:
            addr.append(app_layer)
            offset.append(multSerPacket.get("Offset List").get("cip.msp.offset"))
        else:
            addr.append("0")
            offset.append("0")
        # addr.append(seg.get("cip").get("cip.request_path_size", None))
    ls = np.max([len(sessids),len(cmd),len(length),len(magic),len(timestamp)])
    if ls < 15: continue
    mysets.append(sessids + ["1"])  # Sequence number
    mysets.append([int(t[2:], 16) for t in cmd] + ["2"])  # Function codeFunction code
    mysets.append(length + ["3"])  # Length
    mysets.append([int(t[2:], 16) for t in magic] + ["4"])  # Magic number
    # mysets.append(code_method(sesihandle) + ["1"])
    # mysets.append(code_method(service) + ["2"])
    # mysets.append(timestamp + ["5"])  # Timestamp

# generate_ts_datasets(mysets, "./time_series/without_coding/ctf_cip_4cls.txt")

i = 1
show_samples = 6
# print("Samples number: {}, Aggregation flow: {}".format(len(msgs), len(agg_flow)))
print("Samples number: {}".format(len(newlow)))
# for msg in msgs[start:start + show_samples]:
for msg in [newlow[15]]: # ctf-18
    # origsegs = msg["segments"][:800]
    origsegs = msg
    segs = sorted(origsegs, key=lambda x: x['size'])
    # ########## Cip ######### #
    cmd = []
    length = []
    sessids = []
    magic = []
    status = []
    count = []
    service = []
    addr = []
    sesihandle = []
    senderCtx = []
    timestamp = []
    offset = []
    for seg in segs:
        # "enip"
        magic.append("0x0800")
        cmd.append(seg["enip"]["Encapsulation Header"]["enip.command"])
        length.append(seg["enip"]["Encapsulation Header"]["enip.length"])
        # sessids.append(seg["enip"]["Command Specific Data"]["enip.cpf.itemcount_tree"]["enip.cpf.typeid_tree"]["cip.seq"])
        sessids.append(
            seg.get("enip").get("Command Specific Data").get("enip.cpf.itemcount_tree").get("enip.cpf.typeid_tree").get(
                "cip.seq", "0"))
        timestamp.append(seg.get("enip").get("Command Specific Data").get("enip.time", "0"))
        status.append(seg["enip"]["Encapsulation Header"]["enip.status"])
        count.append(seg["enip"]["Command Specific Data"]["enip.cpf.itemcount"])
        service.append(seg["cip"]["cip.service"])
        sesihandle.append(seg["enip"]["Encapsulation Header"]["enip.context"])

        dt = seg.get("cip")
        app_layer = get_value_from_partial_key(dt, "Request Path")
        multSerPacket = dt.get(get_value_from_partial_key(dt, "Multiple Service Packet"))
        if app_layer is not None and multSerPacket is not None:
            addr.append(app_layer)
            offset.append(multSerPacket.get("Offset List").get("cip.msp.offset"))
        else:
            addr.append("0")
            offset.append("0")
        addr.append(seg.get("cip").get("cip.request_path_size", None))

    # Red:#ef5a28   Purple:#652d90   Green:#4dc3c3  Gray:#828282   Yellow:#FF9F3A
    plt.rcParams['font.sans-serif'] = 'Times New Roman'
    plt.rcParams['font.size'] = 10

    fig, axs = plt.subplots(4, 1, figsize=(6, 4.5))
    axs[0].plot(range(len(sessids)), code_method(sessids), color="#652d90")
    axs[0].set_title("Sequence number")

    axs[1].plot(range(len(cmd)), code_method(cmd), color="#ef5a28")
    axs[1].set_title("Function code")

    axs[2].plot(range(len(length)), code_method(length), color="#4dc3c3", linewidth=2)
    axs[2].set_title("Length")

    axs[3].plot(range(len(magic)), code_method(magic), color="#828282", linewidth=2)
    axs[3].set_title("Magic number")

    for i in [0, 1, 2, 3]:
        axs[i].tick_params(axis='both', which='both', direction='in', bottom=True, top=True, left=True, right=True)
        # axs[i].grid(True, linestyle='--', alpha=0.7)

    plt.subplots_adjust(hspace=0.75)

    plt.show()
