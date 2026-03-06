import json
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from app.core.config import Settings
from app.db.models import Paper
from app.services.data_loader import DataLoader
from app.services.llm_service import LLMService


@dataclass
class BacktestResult:
    title: str
    markdown: str
    metrics: dict[str, float]
    artifact_urls: list[str]


class BacktestService:
    def __init__(self, settings: Settings, llm_service: LLMService) -> None:
        self._settings = settings
        self._llm_service = llm_service
        self._loader = DataLoader()

    def run_for_paper(self, paper: Paper, run_dir: Path, artifact_url_prefix: str = "/artifacts") -> BacktestResult:
        csmar = self._loader.load_csmar(self._settings.csmar_data_path)
        choice = self._loader.load_choice(self._settings.choice_data_path)

        merged = csmar.merge(choice, on=["date", "ticker"], how="inner")
        if merged.empty:
            raise ValueError("CSMAR 与 CHOICE 合并后为空，请检查日期和股票代码对齐。")

        merged = merged.dropna(subset=["ret", "signal"]).copy()
        merged["date"] = pd.to_datetime(merged["date"])
        merged = merged.sort_values(["date", "ticker"])

        merged["bucket"] = merged.groupby("date")["signal"].transform(self._bucketize)
        merged = merged.dropna(subset=["bucket"])
        merged["bucket"] = merged["bucket"].astype(int)

        daily_ret = self._compute_long_short_returns(merged)
        if daily_ret.empty:
            raise ValueError("无法计算多空收益，请检查信号截面覆盖率。")

        equity = (1 + daily_ret).cumprod()
        drawdown = equity / equity.cummax() - 1

        ic_series = (
            merged.groupby("date")
            .apply(
                lambda df: df["signal"].corr(df["ret"], method="spearman") if df["signal"].nunique() > 1 else np.nan,
                include_groups=False,
            )
            .dropna()
        )

        metrics = self._compute_metrics(daily_ret, drawdown, ic_series)

        chart_paths = self._render_charts(run_dir, equity, drawdown, daily_ret)
        artifact_urls = [f"{artifact_url_prefix}/{path.as_posix()}" for path in chart_paths]

        markdown_article = self._build_article(paper, metrics, artifact_urls, ic_series)
        title = f"基于《{paper.title}》的中国市场回测报告"
        return BacktestResult(title=title, markdown=markdown_article, metrics=metrics, artifact_urls=artifact_urls)

    @staticmethod
    def _bucketize(series: pd.Series) -> pd.Series:
        rank = series.rank(method="first")
        try:
            return pd.qcut(rank, 10, labels=False, duplicates="drop")
        except Exception:
            return pd.Series(np.nan, index=series.index)

    @staticmethod
    def _compute_long_short_returns(df: pd.DataFrame) -> pd.Series:
        top_bucket = int(df["bucket"].max())
        bottom_bucket = int(df["bucket"].min())
        grouped = df.groupby(["date", "bucket"])["ret"].mean().unstack()
        if top_bucket not in grouped.columns or bottom_bucket not in grouped.columns:
            return pd.Series(dtype=float)
        long_short = grouped[top_bucket] - grouped[bottom_bucket]
        long_short = long_short.dropna().sort_index()
        long_short.name = "long_short_ret"
        return long_short

    @staticmethod
    def _compute_metrics(daily_ret: pd.Series, drawdown: pd.Series, ic_series: pd.Series) -> dict[str, float]:
        annual_return = float(daily_ret.mean() * 252)
        annual_vol = float(daily_ret.std(ddof=0) * np.sqrt(252))
        sharpe = annual_return / annual_vol if annual_vol > 1e-12 else 0.0
        max_drawdown = float(drawdown.min())
        win_rate = float((daily_ret > 0).mean())
        avg_ic = float(ic_series.mean()) if not ic_series.empty else 0.0
        ic_ir = float(ic_series.mean() / ic_series.std(ddof=0)) if len(ic_series) > 1 and ic_series.std(ddof=0) > 1e-12 else 0.0
        return {
            "annual_return": annual_return,
            "annual_volatility": annual_vol,
            "sharpe_ratio": float(sharpe),
            "max_drawdown": max_drawdown,
            "win_rate": win_rate,
            "avg_ic": avg_ic,
            "ic_ir": ic_ir,
            "trading_days": float(len(daily_ret)),
        }

    @staticmethod
    def _render_charts(run_dir: Path, equity: pd.Series, drawdown: pd.Series, daily_ret: pd.Series) -> list[Path]:
        bt_dir = run_dir / "backtest"
        bt_dir.mkdir(parents=True, exist_ok=True)

        fig1 = bt_dir / "equity_curve.png"
        plt.figure(figsize=(10, 4))
        plt.plot(equity.index, equity.values)
        plt.title("Long-Short Equity Curve")
        plt.xlabel("Date")
        plt.ylabel("Net Value")
        plt.grid(alpha=0.3)
        plt.tight_layout()
        plt.savefig(fig1, dpi=150)
        plt.close()

        fig2 = bt_dir / "drawdown.png"
        plt.figure(figsize=(10, 4))
        plt.fill_between(drawdown.index, drawdown.values, 0, alpha=0.3)
        plt.plot(drawdown.index, drawdown.values, linewidth=1.0)
        plt.title("Drawdown")
        plt.xlabel("Date")
        plt.ylabel("Drawdown")
        plt.grid(alpha=0.3)
        plt.tight_layout()
        plt.savefig(fig2, dpi=150)
        plt.close()

        fig3 = bt_dir / "daily_returns_hist.png"
        plt.figure(figsize=(7, 4))
        plt.hist(daily_ret.values, bins=40, alpha=0.75)
        plt.title("Distribution of Daily Long-Short Returns")
        plt.xlabel("Daily Return")
        plt.ylabel("Frequency")
        plt.tight_layout()
        plt.savefig(fig3, dpi=150)
        plt.close()

        return [fig1.relative_to(run_dir.parent.parent), fig2.relative_to(run_dir.parent.parent), fig3.relative_to(run_dir.parent.parent)]

    def _build_article(
        self,
        paper: Paper,
        metrics: dict[str, float],
        artifact_urls: list[str],
        ic_series: pd.Series,
    ) -> str:
        metrics_json = json.dumps(metrics, ensure_ascii=False, indent=2)
        llm_text = self._llm_service.generate(
            system_prompt="你是量化研究负责人，请写中文、可执行、结构清晰的回测研究文章。",
            user_prompt=(
                f"请基于以下内容输出一篇中文回测文章，格式为 Markdown：\n"
                f"论文标题: {paper.title}\n"
                f"论文摘要: {paper.abstract}\n"
                f"回测指标: {metrics_json}\n"
                f"图像链接: {artifact_urls}\n"
                f"补充要求：必须说明样本构造、交易假设、风险点和下一步实验。"
            ),
        )
        if llm_text:
            return llm_text

        ic_desc = (
            f"IC 均值 {metrics['avg_ic']:.4f}，ICIR {metrics['ic_ir']:.4f}。"
            if not ic_series.empty
            else "IC 无法稳定估计，需提升截面样本规模。"
        )

        return f"""# 基于《{paper.title}》的中国市场回测报告

## 研究目标
将论文思路映射到中国股票市场，以 CSMAR（收益）+ CHOICE（信号）构造日频多空组合，验证策略可行性。

## 数据与方法
- CSMAR: 日频收益数据（`date`,`ticker`,`ret`）  
- CHOICE: 对应信号数据（`date`,`ticker`,`signal`）  
- 每日按信号分十组，做多最高组、做空最低组，得到多空日收益。  

## 回测结果
- 年化收益: {metrics['annual_return']:.2%}
- 年化波动: {metrics['annual_volatility']:.2%}
- Sharpe: {metrics['sharpe_ratio']:.2f}
- 最大回撤: {metrics['max_drawdown']:.2%}
- 胜率: {metrics['win_rate']:.2%}
- {ic_desc}

## 图表
![净值曲线]({artifact_urls[0]})
![回撤曲线]({artifact_urls[1]})
![日收益分布]({artifact_urls[2]})

## 结论与落地建议
1. 若 Sharpe 和回撤达到目标，可进入交易成本与容量约束阶段。  
2. 引入行业中性、市值中性和停牌/涨跌停过滤，提升可交易性。  
3. 将该信号与现有因子做正交化，降低组合相关性。  
"""
