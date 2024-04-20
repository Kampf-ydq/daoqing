# Copyright (C) 2024
# This file includes the following functions:
#   1.constructing a pattern series for each type of field;
#   2.processing flows

from collections import Counter
import numpy as np

# dictionary -> dict {}, partial_key -> str ""
def get_value_from_partial_key(dictionary, partial_key):
    for key in dictionary:
        if partial_key in key:
            return key
    return None


# Remove outliers using IQR
def filter_ts(data):
    # Param data: Array
    Q1 = np.percentile(data, 25)
    Q3 = np.percentile(data, 75)
    IQR = Q3 - Q1
    lower_limit = Q1 - 1.5 * IQR
    upper_limit = Q3 + 1.5 * IQR
    filtered_data = data[(data >= lower_limit) & (data <= upper_limit)]
    return filtered_data


# Code Method: S -> List []
def code_method(S):
    # V = np.unique(S)
    ct = Counter(S)
    V = dict(sorted(ct.items(), key=lambda item: item[1]))
    N = {}
    for i, u in enumerate(V):
        N[u] = i + 1
    ret = []
    for t in S:
        ret.append(N[t])
    if len(V) < 2:
        return ret
    # normalize
    ret = np.array(ret)
    ret = (ret - ret.min()) / ret.ptp()
    # remove outliers
    # ret = filter_ts(ret)
    return ret.tolist()


# Concatenate short flow: msgs -> dict {}
def concate_short_flow(msgs):
    PACKET_NUM = 300
    # signature
    signs = [msg["signature"] for msg in msgs]
    cnt = Counter(signs)
    group_by_sig = {}
    for _, sig in enumerate(cnt):
        segs = []
        for msg in msgs:
            if msg["signature"] == sig:
                segs.append(msg["segments"])
        group_by_sig[sig] = segs

    agg_flow = []
    for _, segs in group_by_sig.items():
        curr_flow = []
        for g in segs:
            if len(curr_flow) <= PACKET_NUM:  # Aggregation
                curr_flow = curr_flow + g
            else:
                agg_flow.append(curr_flow)
                curr_flow = g
        agg_flow.append(curr_flow)
    return agg_flow


def preprocess_flow(msgs):
    PER_FLOW_SIZE = 300

    # Aggregate Short flow
    msgs = concate_short_flow(msgs)

    # Split Long flow
    newflow = []
    time_0 = msgs[0][0]["time"]
    waitlow = []
    for perflow in msgs:
        if len(perflow) <= PER_FLOW_SIZE:
            newflow.append(perflow)
            continue
        for j in range(0, len(perflow), PER_FLOW_SIZE):
            # by flow size
            newflow.append(perflow[j:j + PER_FLOW_SIZE])

            # by time
            # time_1 = msg["time"]
            # if time_1 - time_0 <= 0.01:
            #     waitlow.append(msg)
            # else:
            #     newlow.append(waitlow)
            #     waitlow = [msg]
            #     time_0 = time_1
            # time_0 = time_1
    return newflow


# Build time series datasets
# [[1,2,0,3,"1"],[4,3,2,"2"],[...],...]
def generate_ts_datasets(series, filepath):
    outcnxt = []
    for ses in series:
        val = ses[:-1]
        type = ses[-1]
        outcnxt.append(",".join(str(s) for s in val) + ":" + type)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("\n".join(outcnxt))
        f.close()
    print("over.")