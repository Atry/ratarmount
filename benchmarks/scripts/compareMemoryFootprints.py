#!/usr/bin/env python3

import fnmatch
import os
import re
import sys
from collections import OrderedDict
from itertools import cycle

import matplotlib.pyplot as plt
import numpy as np


def load_memory_tracing_data(fileName):
    labels = None
    pageSize = 4096
    with open(fileName, encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line[0] == '#':
                if labels is None:
                    labels = line[1:].strip().split(' ')
                elif line[1:].strip().split('=')[0] == 'pageSize':
                    pageSize = int(line[1:].strip().split('=')[1])

    assert labels is not None
    assert 'seconds' in labels
    iSeconds = labels.index('seconds')
    assert 'resident' in labels
    iResident = labels.index('resident')

    data = np.genfromtxt(fileName, skip_footer=1).transpose()

    return data[iSeconds] - data[iSeconds][0], data[iResident] * pageSize


def plot_memory_over_time(fileNames, plotFileName='memory-over-time', fileNameSizeMiB=64):
    fig = plt.figure()
    ax = fig.add_subplot(
        111,
        title=f"Approximately {fileNameSizeMiB} MiB File Name Metadata",
        xlabel='Time / s',
        ylabel='Resident Memory / MiB',
    )

    def get_color_group(x):
        return x.split(os.sep)[-1].split('-')[0].split('.')[0]

    colorCycler = cycle(plt.rcParams['axes.prop_cycle'].by_key()['color'])
    groupToColor = {}
    for key in sorted({get_color_group(name) for name in fileNames}):
        if key not in groupToColor:
            groupToColor[key] = next(colorCycler)

    def get_line_style_group(x):
        fileExts = x.split(os.sep)[-1].split('-')[0].split('.')
        if len(fileExts) > 1:
            return fileExts[-1]
        return 'uncompressed'

    groupToLineStyle = {}
    lineCycler = cycle([":", "--", "-", "-."])
    for key in sorted({get_line_style_group(name) for name in fileNames}):
        if key not in groupToLineStyle:
            groupToLineStyle[key] = next(lineCycler)

    maxTime = 0
    maxMemory = 0
    for i in range(len(fileNames)):
        timeSinceStart, memoryInBytes = load_memory_tracing_data(fileNames[i])
        ax.plot(
            timeSinceStart,
            memoryInBytes / 1024.0**2,
            # label = fileNames[i].split( '/' )[-1].split( '-' )[0],
            linestyle=groupToLineStyle[get_line_style_group(fileNames[i])],
            color=groupToColor[get_color_group(fileNames[i])],
        )

        maxTime = np.max([maxTime, np.max(timeSinceStart)])
        maxMemory = np.max([maxMemory, np.max(memoryInBytes) / 1024.0**2])

    for backend, color in groupToColor.items():
        ax.plot([], [], label=backend, color=color)

    for compression, linestyle in groupToLineStyle.items():
        ax.plot([], [], label=compression, color='0.5', linestyle=linestyle)

    ax.set_xlim([0, maxTime])
    ax.set_ylim([0, maxMemory])

    ax.legend(loc='best')
    fig.tight_layout()
    fig.savefig(f'{plotFileName}-{fileNameSizeMiB}-MiB-metadata.png')


def plot_performance_comparison(fileName, fileNameSizeMiB=64):
    widthBetweenBars = 1
    pageSize = 4096

    # load column labels from file
    labels = None  # [ 'tarMiB', 'indexCreationTime', ... ]
    backendLabels = []  # [ 'sqlite', 'custom', 'custom.gz', 'custom.lz2', ... ]
    rawData = []
    with open(fileName, encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line[0] == '#':
                labels = line[1:].strip().split(' ')
            elif '#' in line:
                values, backend = line.split('#', 1)
                rawData += [np.array([float(x) for x in values.split(' ') if x])]
                backendLabels.append(backend.strip())
    rawData = np.array(rawData).transpose()
    (iToCompare,) = np.where(rawData[labels.index('tarMiB')] == fileNameSizeMiB)
    rawData = rawData[:, iToCompare]
    backendLabels = [b for i, b in enumerate(backendLabels) if i in iToCompare]

    compressionGroups = []  # ['', 'gz', 'lz4']
    compressions = []  # ['', '', 'gz', 'lz4', '', 'gz', ... ]
    for groupLabel in backendLabels:
        compression = groupLabel.split('.')[-1] if len(groupLabel.split('.')) >= 2 else ''
        compressions.append(compression)
        if compression not in compressionGroups:
            compressionGroups.append(compression)
    compressionBackgrounds = {'': '', 'gz': '//', 'lz4': 'o'}

    # get unique backend "list" with same ordering as read [ 'custom', 'json', 'sqlite', ... ]
    backends = list(OrderedDict.fromkeys([x.split('.')[0] for x in backendLabels]))

    data = {}
    for iLabel in range(len(labels)):
        data[labels[iLabel]] = rawData[iLabel]

    fig = plt.figure(figsize=(12, 6))

    columnLabels = [
        'indexCreationTime',
        'serializationTime',
        'deserializationTime',
        'serializedSize',
        'peakRssSizeCreation',
        'peakRssSizeLoading',
    ]
    #'peakVmSizeCreation', 'peakVmSizeLoading' ]
    for i in range(len(columnLabels)):
        title = ' '.join(
            [x.capitalize().replace('Rss', 'Resident') for x in re.sub(r'([A-Z])', r' \1', columnLabels[i]).split(' ')]
        )
        ax = fig.add_subplot(
            2,
            3,
            1 + i,
            title=title,
            ylabel="Time / s" if 'Time' in columnLabels[i] else "Size / MiB",
        )

        yData = data[columnLabels[i]]
        if 'Size' in columnLabels[i]:
            yData /= 1024.0**2
            if columnLabels[i] != 'serializedSize':
                yData *= pageSize

        iBest = np.argmin(yData)
        print("Best for", title, ":", backendLabels[iBest], 'with', yData[iBest])

        for j, backend in enumerate(backends):
            bar = None
            for k, compression in enumerate(compressionGroups):
                ys = [
                    y
                    for i, y in enumerate(yData)
                    if backendLabels[i].startswith(backend) and backendLabels[i].endswith(compression or backend)
                ]
                if not ys:
                    continue
                assert len(ys) == 1
                (bar,) = ax.bar(
                    len(compressionGroups) * (j + widthBetweenBars) + k,
                    ys[0],
                    hatch=compressionBackgrounds[compression],
                    color=bar.get_facecolor() if bar else None,
                    label=backend if k == 0 else None,
                )
    print()

    for compression in compressionGroups:
        ax.bar(
            [len(compressionGroups) * widthBetweenBars],
            [0],
            hatch=compressionBackgrounds[compression],
            color='0.9',
            label=compression or 'uncompressed',
        )

    ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))
    fig.tight_layout()
    fig.savefig(f'performance-comparison-{fileNameSizeMiB}-MiB-metadata.png')


if len(sys.argv) != 2 or not os.path.isdir(sys.argv[1]):
    print("First argument must be path to data directory")
    sys.exit(1)

dataFolder = sys.argv[1]
os.chdir(dataFolder)

for sizeMiB in [1, 8, 64, 256]:
    for benchmark in ['saving', 'loading']:
        files = [f for f in os.listdir('.') if fnmatch.fnmatch(f, '*-' + str(sizeMiB) + '-MiB-' + benchmark + '.dat')]
        plot_memory_over_time(files, 'resident-memory-over-time-' + benchmark, sizeMiB)
    try:
        plot_performance_comparison('serializationBenchmark.dat', sizeMiB)
    except Exception as e:
        print("Could not plot performance comparison because:", e)

plt.show()
