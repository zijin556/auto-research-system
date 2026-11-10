"""Plot preserved three-step-demodulation CSV results; never rerun simulation."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(csv_path, summary_path, case):
    summary = json.loads(Path(summary_path).read_text(encoding='utf-8'))
    with Path(csv_path).open(encoding='utf-8', newline='') as stream:
        rows = [{k: float(v) for k, v in row.items()} for row in csv.DictReader(stream)]
    if summary['case'] != case or len(rows) != summary['phase_samples']:
        raise ValueError('Source case or sample count does not match its summary.')
    if any(not math.isfinite(v) for row in rows for v in row.values()):
        raise ValueError('Nonfinite source values.')
    error = max(abs(row['amplitude'] - row['target_amplitude']) for row in rows)
    if not math.isclose(error, summary['max_abs_amplitude_error'], rel_tol=1e-12, abs_tol=1e-16):
        raise ValueError('Source CSV and reported maximum disagree.')
    return rows, summary


def plot(ideal_csv, harmonic_csv, ideal_summary, harmonic_summary, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    names = ['amplitude_and_bias', 'numerical_consistency']
    targets = [output / (name + suffix) for name in names for suffix in ['.png', '.pdf']]
    targets.append(output / 'figure_manifest.json')
    if any(path.exists() for path in targets):
        raise ValueError('Use a new output directory; existing figures are preserved.')
    ideal, si = load(ideal_csv, ideal_summary, 'ideal')
    harmonic, sh = load(harmonic_csv, harmonic_summary, 'harmonic')
    if [r['phi_rad'] for r in ideal] != [r['phi_rad'] for r in harmonic]:
        raise ValueError('Both cases must use the same recorded phase grid.')
    if si['b'] != sh['b'] or si['a'] != sh['a']:
        raise ValueError('Comparisons require the same DC and fundamental amplitude.')
    font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/msyh.ttc'
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        family = font_manager.FontProperties(fname=str(font_path)).get_name()
    else:
        family = 'DejaVu Sans'
    plt.rcParams.update({'font.family': family, 'font.size': 10,
                         'axes.titlesize': 11, 'axes.labelsize': 10,
                         'legend.fontsize': 9, 'axes.unicode_minus': False,
                         'pdf.fonttype': 42, 'ps.fonttype': 42,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'figure.facecolor': 'white', 'axes.facecolor': 'white'})
    x = [r['phi_rad'] / math.pi for r in ideal]
    blue, orange, gray = '#0072B2', '#D55E00', '#555555'
    b, c = sh['b'], sh['c']
    analytic = [math.sqrt(b*b + c*c + 2*b*c*math.cos(3*r['phi_rad'])) for r in harmonic]

    def phase_axis(ax):
        ax.set_xlim(0, 2)
        ax.set_xticks([0, .5, 1, 1.5, 2], ['0', 'π/2', 'π', '3π/2', '2π'])
        ax.set_xlabel('初始相位 φ（rad）')
        ax.grid(axis='y', color='#dce2e8', lw=.6)
        ax.set_axisbelow(True)

    fig, axes = plt.subplots(2, 1, figsize=(8.2, 6.2), layout='constrained')
    ax = axes[0]
    ax.plot(x, [r['amplitude'] for r in ideal], color=blue, lw=1.6,
            label='纯基频：数值恢复（c = 0）')
    ax.plot(x, analytic, color=gray, ls='--', lw=1.5, label='谐波模型：解析预测（式 5）')
    ax.plot(x[::20], [r['amplitude'] for r in harmonic][::20], color=orange,
            ls='none', marker='o', ms=3.8, mfc='white', mew=1.1,
            label='谐波模型：数值恢复（c = 0.2）')
    ax.set_ylim(1.74, 2.27)
    ax.set_yticks([1.8, 2, 2.2])
    ax.set_ylabel('恢复幅值（a.u.）')
    ax.set_title('(a) 幅值恢复：理想条件与谐波反例', loc='left', weight='bold')
    ax.legend(loc='upper center', bbox_to_anchor=(.5, -.27), ncol=2, frameon=False)
    phase_axis(ax)
    ax = axes[1]
    for rows, color, style, label in [(ideal, blue, '--', '纯基频'), (harmonic, orange, '-', '二次谐波')]:
        ax.plot(x, [100*(r['amplitude'] - r['target_amplitude'])/r['target_amplitude'] for r in rows],
                color=color, ls=style, lw=1.5, label=label)
    ax.set_ylim(-12, 12)
    ax.set_yticks([-10, -5, 0, 5, 10])
    ax.set_ylabel('有符号相对幅值误差（%）')
    ax.set_title('(b) 谐波引入随相位变化的系统偏差', loc='left', weight='bold')
    ax.legend(loc='upper center', bbox_to_anchor=(.5, -.25), ncol=2, frameon=False)
    phase_axis(ax)
    fig.savefig(output / 'amplitude_and_bias.png', dpi=300, bbox_inches='tight', pad_inches=.12)
    fig.savefig(output / 'amplitude_and_bias.pdf', bbox_inches='tight', pad_inches=.12)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.4), layout='constrained')
    for ax, field, title in [(axes[0], 'complex_analytic_residual', '(a) 复数解调与解析式的差异'),
                             (axes[1], 'independent_amplitude_difference', '(b) 两种幅值计算路径的差异')]:
        for rows, color, style, label in [(ideal, blue, '-', '纯基频'), (harmonic, orange, '--', '二次谐波')]:
            ax.plot(x, [r[field]/1e-14 for r in rows], color=color, ls=style,
                    lw=.7, alpha=.9, label=label)
        ax.set_ylim(bottom=0)
        ax.set_ylabel(r'数值差异（$\times10^{-14}$ a.u.）')
        ax.set_title(title, loc='left', weight='bold', fontsize=10)
        ax.legend(loc='upper center', bbox_to_anchor=(.5, -.28), ncol=2, frameon=False)
        phase_axis(ax)
    fig.savefig(output / 'numerical_consistency.png', dpi=300, bbox_inches='tight', pad_inches=.12)
    fig.savefig(output / 'numerical_consistency.pdf', bbox_inches='tight', pad_inches=.12)
    plt.close(fig)
    manifest = {
        'purpose': 'Visualization of existing synthetic results; not a new measurement or proof.',
        'matplotlib_version': matplotlib.__version__, 'font': family,
        'inputs': {str(Path(p).resolve()): sha(p) for p in [ideal_csv, harmonic_csv, ideal_summary, harmonic_summary]},
        'phase_samples_per_case': len(ideal), 'phase_interval': '[0, 2*pi), endpoint excluded',
        'marker_stride_figure1a_harmonic': 20, 'statistics_use_all_rows': True,
        'analytic_overlay': 'sqrt(b*b+c*c+2*b*c*cos(3*phi)), using recorded b and c',
        'error_definition': '100*(recovered-target)/target; signed percentage',
        'ideal_max_abs_error': si['max_abs_amplitude_error'],
        'harmonic_amplitude_range': [min(r['amplitude'] for r in harmonic), max(r['amplitude'] for r in harmonic)],
        'harmonic_max_abs_relative_error_percent': 100*sh['max_relative_amplitude_error'],
        'no_error_bars': 'Deterministic phase sweep; no stochastic repeats or estimated confidence intervals.',
        'figures': {p.name: sha(p) for p in targets if p.exists()},
    }
    (output / 'figure_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ['ideal-csv', 'harmonic-csv', 'ideal-summary', 'harmonic-summary', 'output-dir']:
        parser.add_argument('--'+option, required=True, type=Path)
    args = parser.parse_args()
    plot(args.ideal_csv, args.harmonic_csv, args.ideal_summary, args.harmonic_summary, args.output_dir)
