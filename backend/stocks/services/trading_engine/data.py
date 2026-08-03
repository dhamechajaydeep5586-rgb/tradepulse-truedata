from __future__ import annotations

import pandas as pd


def candles_to_dataframe(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    return df
