from collections.abc import Callable, Sequence
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


class StatisticalToolkit:
    """
    A collection of robust statistical tools for the AI Agent.
    Enables 'Scientist' behavior: validating assumptions before modeling.

    Deliberately built on scipy and numpy only -- statsmodels and scikit-learn are for
    *generated* code (see `prompts.TOOLKIT`), not the API process itself; see the "deliberately
    absent" note in requirements.txt. Regression here is therefore closed-form (`np.linalg.lstsq`
    / Newton-Raphson), not a statsmodels/sklearn wrapper.
    """

    @staticmethod
    def check_normality(df: pd.DataFrame, column: str) -> dict[str, Any]:
        """
        Tests if a column is normally distributed using Shapiro-Wilk (N < 5000)
        or D'Agostino's K^2 (N >= 5000).
        """
        data = df[column].dropna()
        n = len(data)

        if n < 3:
            return {"is_normal": False, "p_value": None, "test": "Insufficient Data"}

        if n < 5000:
            # Shapiro-Wilk
            stat, p = stats.shapiro(data)
            test_name = "Shapiro-Wilk"
        else:
            # D'Agostino's K^2
            stat, p = stats.normaltest(data)
            test_name = "D'Agostino's K^2"

        return {
            "column": column,
            "is_normal": bool(p > 0.05),
            "p_value": p,
            "statistic": stat,
            "test_used": test_name,
            "interpretation": "Likely Normal" if p > 0.05 else "Not Normal (Reject Null)",
        }

    @staticmethod
    def detect_outliers(df: pd.DataFrame, column: str, method: str = "iqr") -> dict[str, Any]:
        """
        Detects outliers using IQR or Z-Score.
        """
        data = df[column].dropna()
        outliers = []

        if method == "zscore":
            z_scores = np.abs(stats.zscore(data))
            outliers = data[z_scores > 3].tolist()
            threshold = "Z > 3"
        else:
            # IQR
            Q1 = data.quantile(0.25)
            Q3 = data.quantile(0.75)
            IQR = Q3 - Q1
            lower_bound = Q1 - 1.5 * IQR
            upper_bound = Q3 + 1.5 * IQR
            outliers = data[(data < lower_bound) | (data > upper_bound)].tolist()
            threshold = f"<{lower_bound:.2f} or >{upper_bound:.2f}"

        return {
            "column": column,
            "method": method,
            "outlier_count": len(outliers),
            "outlier_percentage": round(len(outliers) / len(data) * 100, 2) if len(data) > 0 else 0.0,
            "threshold_used": threshold,
            "sample_outliers": outliers[:5],  # Limit output size
        }

    @staticmethod
    def correlation_analysis(df: pd.DataFrame, target_col: str) -> list[dict[str, Any]]:
        """
        Finds features most correlated with the target column.
        Automatically handles numeric conversion for correlation check.
        """
        if target_col not in df.columns:
            return []

        # Select numeric columns
        numeric_df = df.select_dtypes(include=[np.number])
        if target_col not in numeric_df.columns:
            return []  # Target is not numeric?

        correlations = numeric_df.corr()[target_col].drop(target_col)

        # Sort by absolute correlation
        sorted_corr = correlations.abs().sort_values(ascending=False)

        results = []
        for col, val in sorted_corr.items():
            raw_val = correlations[col]
            results.append(
                {
                    "feature": col,
                    "correlation": round(raw_val, 4),
                    "strength": "Strong" if abs(raw_val) > 0.7 else "Moderate" if abs(raw_val) > 0.3 else "Weak",
                }
            )

        return results[:5]  # Top 5

    # ------------------------------------------------------------------ #
    # Effect sizes
    # ------------------------------------------------------------------ #
    @staticmethod
    def cohens_d(a: Sequence[float], b: Sequence[float]) -> float:
        """Standardised mean difference between two independent samples."""
        a_arr, b_arr = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        n1, n2 = len(a_arr), len(b_arr)
        pooled_var = ((n1 - 1) * a_arr.var(ddof=1) + (n2 - 1) * b_arr.var(ddof=1)) / (n1 + n2 - 2)
        pooled_std = np.sqrt(pooled_var)
        return float((a_arr.mean() - b_arr.mean()) / pooled_std) if pooled_std else 0.0

    @staticmethod
    def cliffs_delta(a: Sequence[float], b: Sequence[float]) -> float:
        """Non-parametric effect size: the probability one sample outranks the other, net."""
        a_arr, b_arr = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        if a_arr.size == 0 or b_arr.size == 0:
            return 0.0
        diff = np.subtract.outer(a_arr, b_arr)
        return float((np.sum(diff > 0) - np.sum(diff < 0)) / diff.size)

    @staticmethod
    def cramers_v(confusion: np.ndarray) -> float:
        """Effect size for a chi-square test of independence, bounded to [0, 1]."""
        chi2 = stats.chi2_contingency(confusion, correction=False)[0]
        n = confusion.sum()
        r, k = confusion.shape
        denom = min(k - 1, r - 1)
        return float(np.sqrt((chi2 / n) / denom)) if denom and n else 0.0

    # ------------------------------------------------------------------ #
    # Group comparisons
    # ------------------------------------------------------------------ #
    @staticmethod
    def compare_two_groups(df: pd.DataFrame, value_col: str, group_col: str) -> dict[str, Any]:
        """Independent two-sample test, picking Welch's over Student's when Levene's test says
        the groups' variances differ rather than assuming equal variance by default."""
        groups = list(df[group_col].dropna().unique())
        if len(groups) != 2:
            return {"error": f"expected exactly 2 groups in '{group_col}', found {len(groups)}"}
        a = df.loc[df[group_col] == groups[0], value_col].dropna()
        b = df.loc[df[group_col] == groups[1], value_col].dropna()
        if len(a) < 2 or len(b) < 2:
            return {"error": "each group needs at least 2 observations"}
        _, levene_p = stats.levene(a, b)
        equal_var = bool(levene_p > 0.05)
        stat, p = stats.ttest_ind(a, b, equal_var=equal_var)
        return {
            "test_used": "Student's t-test" if equal_var else "Welch's t-test",
            "group_labels": [str(groups[0]), str(groups[1])],
            "group_sizes": [len(a), len(b)],
            "statistic": float(stat),
            "p_value": float(p),
            "significant": bool(p < 0.05),
            "equal_variance_assumed": equal_var,
            "levene_p_value": float(levene_p),
            "effect_size": {"name": "Cohen's d", "value": round(StatisticalToolkit.cohens_d(a, b), 4)},
        }

    @staticmethod
    def mann_whitney(df: pd.DataFrame, value_col: str, group_col: str) -> dict[str, Any]:
        """Non-parametric alternative to `compare_two_groups`: no normality assumption."""
        groups = list(df[group_col].dropna().unique())
        if len(groups) != 2:
            return {"error": f"expected exactly 2 groups in '{group_col}', found {len(groups)}"}
        a = df.loc[df[group_col] == groups[0], value_col].dropna()
        b = df.loc[df[group_col] == groups[1], value_col].dropna()
        if len(a) < 1 or len(b) < 1:
            return {"error": "each group needs at least 1 observation"}
        stat, p = stats.mannwhitneyu(a, b, alternative="two-sided")
        return {
            "test_used": "Mann-Whitney U",
            "group_labels": [str(groups[0]), str(groups[1])],
            "group_sizes": [len(a), len(b)],
            "statistic": float(stat),
            "p_value": float(p),
            "significant": bool(p < 0.05),
            "effect_size": {"name": "Cliff's delta", "value": round(StatisticalToolkit.cliffs_delta(a, b), 4)},
        }

    @staticmethod
    def paired_comparison(df: pd.DataFrame, col_a: str, col_b: str) -> dict[str, Any]:
        """Paired test between two columns measured on the same rows, picking the paired t-test
        when the differences are normal and Wilcoxon signed-rank otherwise."""
        paired = df[[col_a, col_b]].dropna()
        if len(paired) < 3:
            return {"error": "paired comparison needs at least 3 complete pairs"}
        diff = paired[col_a] - paired[col_b]
        normal = bool(StatisticalToolkit.check_normality(pd.DataFrame({"d": diff}), "d")["is_normal"])
        if normal:
            stat, p = stats.ttest_rel(paired[col_a], paired[col_b])
            test_used = "Paired t-test"
        else:
            stat, p = stats.wilcoxon(paired[col_a], paired[col_b])
            test_used = "Wilcoxon signed-rank"
        return {
            "test_used": test_used,
            "n_pairs": len(diff),
            "mean_difference": float(diff.mean()),
            "statistic": float(stat),
            "p_value": float(p),
            "significant": bool(p < 0.05),
            "normality_assumed": normal,
        }

    @staticmethod
    def one_way_anova(df: pd.DataFrame, value_col: str, group_col: str) -> dict[str, Any]:
        """Compares 3+ groups, falling back to Kruskal-Wallis when normality or equal variance
        does not hold across every group."""
        groups = [g.dropna().to_numpy(dtype=float) for _, g in df.groupby(group_col)[value_col]]
        groups = [g for g in groups if len(g) >= 2]
        if len(groups) < 3:
            return {"error": f"one-way ANOVA needs at least 3 groups with 2+ observations, found {len(groups)}"}
        normal = all(StatisticalToolkit.check_normality(pd.DataFrame({"x": g}), "x")["is_normal"] for g in groups)
        _, levene_p = stats.levene(*groups)
        equal_var = bool(levene_p > 0.05)
        if normal and equal_var:
            stat, p = stats.f_oneway(*groups)
            test_used = "One-way ANOVA"
        else:
            stat, p = stats.kruskal(*groups)
            test_used = "Kruskal-Wallis"
        pooled = np.concatenate(groups)
        grand_mean = pooled.mean()
        ss_between = sum(len(g) * (g.mean() - grand_mean) ** 2 for g in groups)
        ss_total = float(((pooled - grand_mean) ** 2).sum())
        eta_squared = float(ss_between / ss_total) if ss_total else 0.0
        return {
            "test_used": test_used,
            "group_count": len(groups),
            "statistic": float(stat),
            "p_value": float(p),
            "significant": bool(p < 0.05),
            "normality_assumed": normal,
            "equal_variance_assumed": equal_var,
            "effect_size": {"name": "eta-squared", "value": round(eta_squared, 4)},
        }

    @staticmethod
    def chi_square_test(df: pd.DataFrame, col_a: str, col_b: str) -> dict[str, Any]:
        """Chi-square test of independence between two categorical columns."""
        table = pd.crosstab(df[col_a], df[col_b])
        if table.shape[0] < 2 or table.shape[1] < 2:
            return {"error": "chi-square needs at least two categories in each column"}
        chi2, p, dof, expected = stats.chi2_contingency(table)
        return {
            "test_used": "Chi-square test of independence",
            "statistic": float(chi2),
            "p_value": float(p),
            "degrees_of_freedom": int(dof),
            "significant": bool(p < 0.05),
            "low_expected_count_warning": bool((expected < 5).any()),
            "effect_size": {"name": "Cramer's V", "value": round(StatisticalToolkit.cramers_v(table.to_numpy()), 4)},
        }

    # ------------------------------------------------------------------ #
    # Correlation
    # ------------------------------------------------------------------ #
    @staticmethod
    def correlation_with_ci(
        df: pd.DataFrame, col_a: str, col_b: str, method: str = "pearson", confidence: float = 0.95
    ) -> dict[str, Any]:
        """Correlation coefficient with a Fisher-z confidence interval."""
        paired = df[[col_a, col_b]].dropna()
        n = len(paired)
        if n < 4:
            return {"error": "correlation needs at least 4 paired observations"}
        if method == "spearman":
            r, p = stats.spearmanr(paired[col_a], paired[col_b])
        else:
            r, p = stats.pearsonr(paired[col_a], paired[col_b])
            method = "pearson"
        z = np.arctanh(np.clip(r, -0.9999, 0.9999))
        se = 1 / np.sqrt(n - 3)
        z_crit = stats.norm.ppf(1 - (1 - confidence) / 2)
        lower, upper = np.tanh(z - z_crit * se), np.tanh(z + z_crit * se)
        return {
            "method": method,
            "n": n,
            "r": float(r),
            "p_value": float(p),
            "confidence_interval": [float(lower), float(upper)],
            "significant": bool(p < 0.05),
        }

    # ------------------------------------------------------------------ #
    # Regression
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ols_r_squared(y: pd.Series, x: pd.DataFrame) -> float:
        design = np.column_stack([np.ones(len(x)), x.to_numpy(dtype=float)])
        y_arr = y.to_numpy(dtype=float)
        coefs, *_ = np.linalg.lstsq(design, y_arr, rcond=None)
        fitted = design @ coefs
        ss_res = float(((y_arr - fitted) ** 2).sum())
        ss_tot = float(((y_arr - y_arr.mean()) ** 2).sum())
        return 1 - ss_res / ss_tot if ss_tot else 0.0

    @staticmethod
    def _variance_inflation_factors(features_df: pd.DataFrame) -> dict[str, float]:
        """How much each predictor's variance is inflated by its correlation with the others."""
        vif: dict[str, float] = {}
        columns = list(features_df.columns)
        for column in columns:
            others = [c for c in columns if c != column]
            if not others:
                vif[column] = 1.0
                continue
            r_squared = StatisticalToolkit._ols_r_squared(features_df[column], features_df[others])
            vif[column] = float("inf") if r_squared >= 1 else round(1 / (1 - r_squared), 4)
        return vif

    @staticmethod
    def linear_regression(df: pd.DataFrame, target: str, features: Sequence[str]) -> dict[str, Any]:
        """OLS regression via closed-form least squares, with the diagnostics a naive fit skips:
        per-coefficient significance, overall F-test, residual normality and multicollinearity."""
        features = list(features)
        data = df[[target, *features]].dropna()
        n, k = len(data), len(features)
        if n <= k + 1:
            return {"error": f"linear regression needs more rows ({n}) than predictors plus one ({k + 1})"}
        design = np.column_stack([np.ones(n), data[features].to_numpy(dtype=float)])
        y = data[target].to_numpy(dtype=float)
        coefs, *_ = np.linalg.lstsq(design, y, rcond=None)
        fitted = design @ coefs
        resid = y - fitted
        dof_resid = n - k - 1
        mse = float((resid**2).sum() / dof_resid) if dof_resid > 0 else float("nan")
        xtx_inv = np.linalg.pinv(design.T @ design)
        se = np.sqrt(np.diag(xtx_inv) * mse)
        t_stats = np.divide(coefs, se, out=np.zeros_like(coefs), where=se != 0)
        p_values = 2 * (1 - stats.t.cdf(np.abs(t_stats), dof_resid)) if dof_resid > 0 else np.full_like(coefs, np.nan)
        ss_total = float(((y - y.mean()) ** 2).sum())
        ss_resid = float((resid**2).sum())
        r_squared = 1 - ss_resid / ss_total if ss_total else 0.0
        adj_r_squared = 1 - (1 - r_squared) * (n - 1) / dof_resid if dof_resid > 0 else None
        f_stat = ((ss_total - ss_resid) / k) / mse if k and mse else float("nan")
        f_p_value = float(1 - stats.f.cdf(f_stat, k, dof_resid)) if dof_resid > 0 and k else None
        coefficients = [
            {
                "term": "intercept" if i == 0 else features[i - 1],
                "coefficient": float(coefs[i]),
                "std_error": float(se[i]),
                "t_statistic": float(t_stats[i]),
                "p_value": float(p_values[i]),
            }
            for i in range(len(coefs))
        ]
        residual_normal = bool(StatisticalToolkit.check_normality(pd.DataFrame({"resid": resid}), "resid")["is_normal"])
        return {
            "n": n,
            "r_squared": round(r_squared, 4),
            "adj_r_squared": round(adj_r_squared, 4) if adj_r_squared is not None else None,
            "f_statistic": float(f_stat) if dof_resid > 0 else None,
            "f_p_value": f_p_value,
            "coefficients": coefficients,
            "residual_normality": residual_normal,
            "multicollinearity": StatisticalToolkit._variance_inflation_factors(data[features]) if k > 1 else {},
        }

    @staticmethod
    def logistic_regression(
        df: pd.DataFrame, target: str, features: Sequence[str], max_iter: int = 50, tol: float = 1e-8
    ) -> dict[str, Any]:
        """Binary logistic regression fit by Newton-Raphson (IRLS), with odds ratios, Wald
        significance per coefficient and McFadden's pseudo-R-squared."""
        features = list(features)
        data = df[[target, *features]].dropna()
        classes = sorted(data[target].unique(), key=str)
        if len(classes) != 2:
            return {"error": f"logistic regression needs a binary target, found {len(classes)} classes"}
        n, k = len(data), len(features)
        if n <= k + 1:
            return {"error": f"logistic regression needs more rows ({n}) than predictors plus one ({k + 1})"}
        y = (data[target] == classes[1]).to_numpy(dtype=float)
        design = np.column_stack([np.ones(n), data[features].to_numpy(dtype=float)])
        beta = np.zeros(design.shape[1])
        for _ in range(max_iter):
            probabilities = 1 / (1 + np.exp(-(design @ beta)))
            weights = np.clip(probabilities * (1 - probabilities), 1e-9, None)
            gradient = design.T @ (y - probabilities)
            hessian = -(design * weights[:, None]).T @ design
            try:
                step = np.linalg.solve(hessian, gradient)
            except np.linalg.LinAlgError:
                return {"error": "logistic regression failed to converge (singular design matrix)"}
            beta_new = beta - step
            converged = np.max(np.abs(beta_new - beta)) < tol
            beta = beta_new
            if converged:
                break
        probabilities = 1 / (1 + np.exp(-(design @ beta)))
        log_likelihood = float(
            np.sum(
                y * np.log(np.clip(probabilities, 1e-12, 1)) + (1 - y) * np.log(np.clip(1 - probabilities, 1e-12, 1))
            )
        )
        null_p = float(y.mean())
        null_ll = float(n * (null_p * np.log(null_p) + (1 - null_p) * np.log(1 - null_p))) if 0 < null_p < 1 else 0.0
        mcfadden_r_squared = 1 - log_likelihood / null_ll if null_ll else 0.0
        weights = np.clip(probabilities * (1 - probabilities), 1e-9, None)
        covariance = np.linalg.pinv((design * weights[:, None]).T @ design)
        se = np.sqrt(np.diag(covariance))
        z_stats = np.divide(beta, se, out=np.zeros_like(beta), where=se != 0)
        p_values = 2 * (1 - stats.norm.cdf(np.abs(z_stats)))
        coefficients = [
            {
                "term": "intercept" if i == 0 else features[i - 1],
                "coefficient": float(beta[i]),
                "odds_ratio": float(np.exp(beta[i])),
                "std_error": float(se[i]),
                "z_statistic": float(z_stats[i]),
                "p_value": float(p_values[i]),
            }
            for i in range(len(beta))
        ]
        predicted = (probabilities >= 0.5).astype(int)
        return {
            "n": n,
            "positive_class": str(classes[1]),
            "mcfadden_r_squared": round(mcfadden_r_squared, 4),
            "log_likelihood": log_likelihood,
            "coefficients": coefficients,
            "accuracy": round(float((predicted == y.astype(int)).mean()), 4),
        }

    # ------------------------------------------------------------------ #
    # Resampling
    # ------------------------------------------------------------------ #
    @staticmethod
    def bootstrap_ci(
        data: Sequence[float],
        statistic: Callable[[np.ndarray], float] = np.mean,
        n_resamples: int = 2000,
        confidence: float = 0.95,
        seed: int = 42,
    ) -> dict[str, Any]:
        """Percentile bootstrap confidence interval for an arbitrary statistic."""
        values = np.asarray(pd.Series(data).dropna(), dtype=float)
        if len(values) < 2:
            return {"error": "bootstrap needs at least 2 observations"}
        result = stats.bootstrap(
            (values,),
            statistic,
            n_resamples=n_resamples,
            confidence_level=confidence,
            random_state=np.random.default_rng(seed),
            method="percentile",
        )
        return {
            "statistic": float(statistic(values)),
            "confidence_interval": [float(result.confidence_interval.low), float(result.confidence_interval.high)],
            "n_resamples": n_resamples,
        }

    @staticmethod
    def permutation_test(
        a: Sequence[float],
        b: Sequence[float],
        statistic: Callable[[np.ndarray, np.ndarray], float] | None = None,
        n_resamples: int = 2000,
        seed: int = 42,
    ) -> dict[str, Any]:
        """Permutation test for a difference between two samples, with no distributional assumption."""
        a_arr = np.asarray(pd.Series(a).dropna(), dtype=float)
        b_arr = np.asarray(pd.Series(b).dropna(), dtype=float)
        if len(a_arr) < 2 or len(b_arr) < 2:
            return {"error": "permutation test needs at least 2 observations per group"}
        stat_fn = statistic or (lambda x, y: np.mean(x) - np.mean(y))
        result = stats.permutation_test(
            (a_arr, b_arr),
            stat_fn,
            n_resamples=n_resamples,
            random_state=np.random.default_rng(seed),
            alternative="two-sided",
        )
        return {
            "statistic": float(result.statistic),
            "p_value": float(result.pvalue),
            "significant": bool(result.pvalue < 0.05),
            "n_resamples": n_resamples,
        }

    # ------------------------------------------------------------------ #
    # Multiple comparisons / model evaluation
    # ------------------------------------------------------------------ #
    @staticmethod
    def correct_p_values(p_values: Sequence[float], method: str = "fdr_bh", alpha: float = 0.05) -> dict[str, Any]:
        """Adjusts a family of p-values for multiple comparisons (Bonferroni or Benjamini-Hochberg)."""
        p = np.asarray(p_values, dtype=float)
        m = len(p)
        if m == 0:
            return {"method": method, "adjusted_p_values": [], "rejected": []}
        if method == "bonferroni":
            adjusted = np.clip(p * m, 0, 1)
        else:
            method = "fdr_bh"
            order = np.argsort(p)
            ranked = p[order]
            adjusted_sorted = np.minimum.accumulate((ranked * m / (np.arange(m) + 1))[::-1])[::-1]
            adjusted = np.empty(m)
            adjusted[order] = np.clip(adjusted_sorted, 0, 1)
        return {
            "method": method,
            "adjusted_p_values": [round(float(v), 6) for v in adjusted],
            "rejected": [bool(v < alpha) for v in adjusted],
        }

    @staticmethod
    def evaluate_classifier(y_true: Sequence[Any], y_pred: Sequence[Any]) -> dict[str, Any]:
        """Confusion-matrix-derived metrics for a binary classifier, no sklearn dependency."""
        true_arr, pred_arr = np.asarray(y_true), np.asarray(y_pred)
        if len(true_arr) != len(pred_arr) or len(true_arr) == 0:
            return {"error": "y_true and y_pred must be non-empty and of equal length"}
        labels = sorted(set(true_arr) | set(pred_arr), key=str)
        if len(labels) != 2:
            return {"error": f"evaluate_classifier expects a binary outcome, found {len(labels)} labels"}
        positive = labels[1]
        tp = int(np.sum((pred_arr == positive) & (true_arr == positive)))
        tn = int(np.sum((pred_arr != positive) & (true_arr != positive)))
        fp = int(np.sum((pred_arr == positive) & (true_arr != positive)))
        fn = int(np.sum((pred_arr != positive) & (true_arr == positive)))
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        return {
            "positive_class": str(positive),
            "confusion_matrix": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
            "accuracy": round((tp + tn) / len(true_arr), 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
