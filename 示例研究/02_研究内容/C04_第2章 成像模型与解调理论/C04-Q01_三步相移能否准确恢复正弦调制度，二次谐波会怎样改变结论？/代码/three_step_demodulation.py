"""A deterministic synthetic check of three-step phase demodulation.

Usage: python three_step_demodulation.py --case ideal|harmonic --output-dir DIR
No real instrument data are used. Do not overwrite a previous run directory.
"""
import argparse
import cmath
import csv
import json
import math
from pathlib import Path


def synthesize(phi, a, b, c):
    return [a + b * math.cos(phi + theta)
            + c * math.cos(2 * phi + 2 * theta)
            for theta in [0, 2 * math.pi / 3, 4 * math.pi / 3]]


def demodulate(intensities):
    return (2 / 3) * sum(value * cmath.exp(-1j * 2 * math.pi * k / 3)
                         for k, value in enumerate(intensities))


def amplitude_from_differences(intensities):
    # Independent real-valued algebraic expression, no complex Fourier sum.
    first, second, third = intensities
    return math.sqrt((2 / 9) * ((first - second) ** 2
                              + (second - third) ** 2
                              + (third - first) ** 2))


def run(case, output):
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ['samples.csv', 'summary.json']):
        raise ValueError('Choose a fresh output directory; previous outputs are immutable.')
    a, b, c, count, tolerance = 100.0, 2.0, (0.0 if case == 'ideal' else 0.2), 720, 1e-10
    rows = []
    for index in range(count):
        phi = 2 * math.pi * index / count
        intensities = synthesize(phi, a, b, c)
        recovered = demodulate(intensities)
        analytic = b * cmath.exp(1j * phi) + c * cmath.exp(-2j * phi)
        independent = amplitude_from_differences(intensities)
        rows.append({'index': index, 'phi_rad': phi, 'I0': intensities[0],
                     'I1': intensities[1], 'I2': intensities[2], 'amplitude': abs(recovered),
                     'target_amplitude': b, 'amplitude_error': abs(recovered) - b,
                     'complex_analytic_residual': abs(recovered - analytic),
                     'independent_amplitude_difference': abs(abs(recovered) - independent)})
    with (output / 'samples.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    error = max(abs(row['amplitude_error']) for row in rows)
    analytic_error = max(row['complex_analytic_residual'] for row in rows)
    independent_error = max(row['independent_amplitude_difference'] for row in rows)
    bounds = [min(row['amplitude'] for row in rows), max(row['amplitude'] for row in rows)]
    # Frozen before execution: exact-model recovery and explicit boundary check.
    passed = (error < tolerance if case == 'ideal' else
              error > 0.19 and abs(bounds[0] - 1.8) < tolerance and abs(bounds[1] - 2.2) < tolerance)
    passed = passed and analytic_error < tolerance and independent_error < tolerance
    summary = {'case': case, 'a': a, 'b': b, 'c': c, 'phase_samples': count,
               'tolerance': tolerance, 'amplitude_min': bounds[0], 'amplitude_max': bounds[1],
               'max_abs_amplitude_error': error, 'max_relative_amplitude_error': error / b,
               'max_complex_analytic_residual': analytic_error,
               'max_independent_amplitude_difference': independent_error,
               'criterion_met': passed,
               'scope': 'Synthetic exact three-sample model; no noise, phase error or instrument data.'}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['ideal', 'harmonic'], required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.case, args.output_dir))
