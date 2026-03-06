from pathlib import Path

import pandas as pd


class DataLoader:
    def load_csmar(self, path: str) -> pd.DataFrame:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"CSMAR 数据文件不存在: {file_path}")

        if file_path.suffix.lower() in {".parquet", ".pq"}:
            df = pd.read_parquet(file_path)
        else:
            df = pd.read_csv(file_path)

        required_cols = {"date", "ticker"}
        if not required_cols.issubset(df.columns):
            raise ValueError("CSMAR 数据至少需要包含列: date, ticker")

        if "ret" not in df.columns:
            if "close" not in df.columns:
                raise ValueError("CSMAR 数据缺少 ret 或 close 列，无法计算收益。")
            df = df.sort_values(["ticker", "date"])
            df["ret"] = df.groupby("ticker")["close"].pct_change()

        df["date"] = pd.to_datetime(df["date"])
        df = df.dropna(subset=["ret"])
        return df[["date", "ticker", "ret"]].copy()

    def load_choice(self, path: str) -> pd.DataFrame:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"CHOICE 数据文件不存在: {file_path}")

        if file_path.suffix.lower() in {".parquet", ".pq"}:
            df = pd.read_parquet(file_path)
        else:
            df = pd.read_csv(file_path)

        required_cols = {"date", "ticker", "signal"}
        if not required_cols.issubset(df.columns):
            raise ValueError("CHOICE 数据必须包含列: date, ticker, signal")

        df["date"] = pd.to_datetime(df["date"])
        df["signal"] = pd.to_numeric(df["signal"], errors="coerce")
        df = df.dropna(subset=["signal"])
        return df[["date", "ticker", "signal"]].copy()
