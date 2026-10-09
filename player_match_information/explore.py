import pandas as pd
import matplotlib.pyplot as plt

from features import *


def plot_missing_features_over_time(df, missing_features):
    # plot when features become available
    df_missing = df.copy()
    df_missing["date"] = pd.to_datetime(df_missing["date"])
    time_df = df_missing.set_index("date")

    num_features = len(missing_features)
    ncols = 2
    nrows = (num_features + ncols - 1) // ncols

    fig, axes = plt.subplots(
        nrows=nrows, ncols=ncols, figsize=(16, 3 * nrows), sharex=True
    )

    flat_axes = axes.flatten()

    for ax, col in zip(flat_axes, missing_features):
        if col in time_df.columns:
            # Calculate weekly non-null sums
            weekly_data = time_df[col].notnull().resample("W").sum()

            # Plot timeline trend
            weekly_data.plot(ax=ax, kind="line", marker="o", color="#2b5c8f")
            ax.set_ylabel("Player Count", fontsize=10)
            ax.set_title(f"Data Availability: {col}", fontsize=11, loc="left", fontweight="bold")
            ax.grid(True, linestyle="--", alpha=0.5)
        else:
            ax.text(
                0.5,
                0.5,
                f"Column '{col}' not found",
                transform=ax.transAxes,
                ha="center",
                va="center",
            )

    for i in range(num_features, len(flat_axes)):
        fig.delaxes(flat_axes[i])

    plt.xlabel("Date (Weekly Intervals)", fontsize=11)
    plt.tight_layout()
    plt.show()


# all players player_stats
player_df = pd.read_csv('data/premier-league/2024-2025/player_match_stats.csv')
print('Shape: ', player_df.shape)
print(player_df.columns.tolist())

# attackers only
attackers = player_df.loc[player_df['position'].isin(['M', 'F'])]
attackers = attackers.drop(columns=gk_features)

# remove unused subs
attackers = attackers.loc[attackers['minutes_played'] > 0]
# print('Attackers shape: ', attackers.shape)

# show missing records per column
missing_counts = attackers.isnull().sum().sort_values(ascending=False)
# print(missing_counts.to_string())

# plot_missing_features_over_time(attackers, missing_features)

# missing_df = attackers[identifiers + missing_features]
# missing_df.to_csv('missing.csv', index=False)
