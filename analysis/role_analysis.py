import matplotlib.pyplot as plt
import pandas as pd

from analysis.features import *


def plot_role_boxplots(df, role_metrics):
    for category, metrics in role_metrics.items():

        # Only keep columns that actually exist
        available_metrics = [
            metric for metric in metrics
            if metric in df.columns
        ]

        if not available_metrics:
            continue

        # Remove columns that are entirely NaN
        available_metrics = [
            metric for metric in available_metrics
            if df[metric].notna().any()
        ]

        if not available_metrics:
            continue

        plot_data = [
            df[metric].dropna()
            for metric in available_metrics
        ]

        fig, ax = plt.subplots(
            figsize=(10, max(4, len(available_metrics) * 0.6))
        )

        ax.boxplot(
            plot_data,
            vert=False,
            tick_labels=available_metrics,
            showfliers=True
        )

        ax.set_title(f"{category.title()} Role Metrics")
        ax.set_xlabel("Value")
        ax.set_ylabel("Metric")

        plt.tight_layout()
        plt.show()


# all players player_stats
player_df = pd.read_csv('data/premier-league/2024-2025/player_match_stats.csv')

# attackers only
attackers = player_df.loc[player_df['position'].isin(['M', 'F'])]
attackers = attackers.drop(columns=gk_features)
attackers = attackers.loc[attackers['minutes_played'] > 0]
print('Shape: ', attackers.shape)
# print(attackers.columns.tolist())

plot_role_boxplots(attackers, ROLE)
