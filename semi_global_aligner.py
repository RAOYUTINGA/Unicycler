#!/usr/bin/env python
'''
Semi-global long read aligner

This is a script to align error-prone long reads (e.g. PacBio or Nanopore) to one or more
references in a semi-global manner. Semi-global alignment allows for unpenalised end gaps, but the
alignment will continue until one of the two sequences ends. This includes cases where the two
sequences overlap and cases where one sequence is contained within the other:

  AAAAA        AAAAAAAAAAA         AAAAAAAA     AAAAAAAA
  |||||          |||||||           |||||           |||||
BBBBBBBBB        BBBBBBB       BBBBBBBBB           BBBBBBBBB

This tool is intended for cases where the reads and reference are expected to match perfectly (or
at least as perfectly as error-prone long reads can match). An example of an appropriate case would
be if the reference sequences are assembled contigs of a bacterial strain and the long reads are
from the same strain.

Required inputs:
  1) FASTA file of one or more reference sequences
  2) FASTQ file of long reads

Output: SAM file of alignments

Author: Ryan Wick
email: rrwick@gmail.com
'''
from __future__ import print_function
from __future__ import division

import subprocess
import sys
import os
import re
import random
import argparse
import time
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing import cpu_count
import threading

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(SCRIPT_DIR, 'lib'))
from misc import int_to_str, float_to_str, check_file_exists, quit_with_error, check_graphmap
from cpp_function_wrappers import semi_global_alignment, new_kmer_positions, add_kmer_positions, \
                                  delete_all_kmer_positions, start_extension_alignment, \
                                  end_extension_alignment, \
                                  get_random_sequence_alignment_mean_and_std_dev

# Used to ensure that multiple threads writing to the same SAM file don't write at the same time.
sam_write_lock = threading.Lock()

'''
VERBOSITY controls how much the script prints to the screen.
0 = nothing is printed
1 = a relatively simple output is printed
2 = a more thorough output is printed, including details on each Seqan alignment
3 = even more output is printed, including stuff from the C++ code
4 = tons of stuff is printed, including all k-mer positions in each Seqan alignment
'''
VERBOSITY = 0

'''
EXPECTED_SLOPE is the anticipated reference to read ratio. It is used by the C++ Seqan code to
rotate the common k-mer rectangles when looking for alignment lines. It is a global because it will
be constantly updated as reads are aligned.
TOTAL_REF_LENGTH and TOTAL_READ_LENGTH are the totals used to calculate EXPECTED_SLOPE. They start
at 10000 (not the more literal value of 0) so EXPECTED_SLOPE isn't too prone to fluctuation at the
start.
'''
EXPECTED_SLOPE = 1.0
TOTAL_REF_LENGTH = 10000
TOTAL_READ_LENGTH = 10000


def main():
    '''
    If this script is run on its own, execution starts here.
    '''
    full_command = ' '.join(sys.argv)
    args = get_arguments()
    check_file_exists(args.ref)
    check_file_exists(args.reads)
    if not args.no_graphmap:
        check_graphmap(args.graphmap_path)

    references = load_references(args.ref, VERBOSITY)
    read_dict, read_names = load_long_reads(args.reads, VERBOSITY)
    scoring_scheme = AlignmentScoringScheme(args.scores)

    semi_global_align_long_reads(references, args.ref, read_dict, read_names, args.reads,
                                 args.temp_dir, args.graphmap_path, args.threads, scoring_scheme,
                                 args.low_score, not args.no_graphmap, args.keep_bad, args.kmer,
                                 args.min_len, args.sam, full_command, args.allowed_overlap,
                                 VERBOSITY)
    sys.exit(0)

def get_arguments():
    '''
    Specifies the command line arguments required by the script.
    '''
    parser = argparse.ArgumentParser(description='Semi-global long read aligner',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--ref', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTA file containing one or more reference sequences')
    parser.add_argument('--reads', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTQ file of long reads')
    parser.add_argument('--sam', type=str, required=True, default=argparse.SUPPRESS,
                        help='SAM file of resulting alignments')
    
    add_aligning_arguments(parser, False)

    parser.add_argument('--threads', type=int, required=False, default=argparse.SUPPRESS,
                        help='Number of CPU threads used to align (default: the number of '
                             'available CPUs)')
    parser.add_argument('--verbosity', type=int, required=False, default=1,
                        help='Level of stdout information (0 to 4)')

    args = parser.parse_args()

    global VERBOSITY
    VERBOSITY = args.verbosity

    fix_up_arguments(args)

    return args

def add_aligning_arguments(parser, hide_help):
    '''
    Adds the aligning-specific arguments to the parser.
    '''
    temp_dir_help = argparse.SUPPRESS if hide_help else 'Temp directory for working files ' + \
                                                        '("PID" will be replaced with the ' + \
                                                        'process ID)'
    parser.add_argument('--temp_dir', type=str, required=False, default='align_temp_PID',
                        help=temp_dir_help)

    no_graphmap_help = argparse.SUPPRESS if hide_help else 'Do not use GraphMap as a ' + \
                                                           'first-pass aligner (default: ' + \
                                                           'GraphMap is used)'
    parser.add_argument('--no_graphmap', action='store_true', default=argparse.SUPPRESS,
                        help=no_graphmap_help)

    graphmap_path_help = argparse.SUPPRESS if hide_help else 'Path to the GraphMap executable'
    parser.add_argument('--graphmap_path', type=str, required=False, default='graphmap',
                        help=graphmap_path_help)

    scores_help = argparse.SUPPRESS if hide_help else 'Comma-delimited string of alignment ' + \
                                                      'scores: match, mismatch, gap open, gap ' + \
                                                      'extend'
    parser.add_argument('--scores', type=str, required=False, default='3,-6,-5,-2',
                        help=scores_help)

    low_score_help = argparse.SUPPRESS if hide_help else 'Score threshold - alignments below ' + \
                                                         'this are considered poor (default: ' + \
                                                         'set threshold automatically)'
    parser.add_argument('--low_score', type=float, required=False, default=argparse.SUPPRESS,
                        help=low_score_help)

    min_len_help = argparse.SUPPRESS if hide_help else 'Minimum alignment length (bp) - ' + \
                                                       'exclude alignments shorter than this ' + \
                                                       'length'
    parser.add_argument('--min_len', type=float, required=False, default=100,
                        help=min_len_help)

    keep_bad_help = argparse.SUPPRESS if hide_help else 'Include alignments in the results ' + \
                                                        'even if they are below the low score ' + \
                                                        'threshold (default: low-scoring ' + \
                                                        'alignments are discarded)'
    parser.add_argument('--keep_bad', action='store_true', default=argparse.SUPPRESS,
                        help=keep_bad_help)

    allowed_overlap_help = argparse.SUPPRESS if hide_help else 'Allow this much overlap ' + \
                                                               'between alignments in a ' + \
                                                               'single read'
    parser.add_argument('--allowed_overlap', type=int, required=False, default=100,
                        help=allowed_overlap_help)

    kmer_help = argparse.SUPPRESS if hide_help else 'K-mer size used for seeding alignments'
    parser.add_argument('--kmer', type=int, required=False, default=7,
                        help=kmer_help)

def fix_up_arguments(args):
    '''
    Repairs issues with the arguments, like not existing. We don't use None/False as a default
    in add_argument because it makes the help text look weird.
    '''
    try:
        args.low_score
    except AttributeError:
        args.low_score = None
    try:
        args.no_graphmap
    except AttributeError:
        args.no_graphmap = False
    try:
        args.keep_bad
    except AttributeError:
        args.keep_bad = False
    try:
        args.threads
    except AttributeError:
        args.threads = cpu_count()
        if VERBOSITY > 2:
            print('\nThread count set to', args.threads)

    # Add the process ID to the default temp directory so multiple instances can run at once in the
    # same directory.
    args.temp_dir = args.temp_dir.replace('PID', str(os.getpid()))

def semi_global_align_long_reads(references, ref_fasta, read_dict, read_names, reads_fastq,
                                 temp_dir, graphmap_path, threads, scoring_scheme,
                                 low_score_threshold, use_graphmap, keep_bad, kmer_size,
                                 min_align_length, sam_filename, full_command, allowed_overlap,
                                 verbosity=None):
    '''
    This function does the primary work of this module: aligning long reads to references in an
    end-gap-free, semi-global manner. It returns a list of Read objects which contain their
    alignments.
    If seqan_all is True, then every Alignment object will be refined by using Seqan.
    If seqan_all is False, then only the overlap alignments and a small set of long contained
    alignments will be run through Seqan.
    '''
    if verbosity:
        global VERBOSITY
        VERBOSITY = verbosity
        
    # If the user supplied a low score threshold, we use that. Otherwise, we'll use the median
    # score minus three times the MAD.
    if VERBOSITY > 0:
        print('Determining low-score threshold')
        print('-------------------------------')
    if low_score_threshold:
        if VERBOSITY > 0:
            print('Using user-supplied low score threshold: ' +
                  float_to_str(low_score_threshold, 2) + '\n')
    else:
        if VERBOSITY > 0:
            print('Automatically choosing a low score threshold using random alignments.\n')
        std_devs_over_mean = 5
        low_score_threshold, rand_mean, rand_std_dev = get_auto_score_threshold(scoring_scheme,
                                                                                std_devs_over_mean)
        if VERBOSITY > 0:
            print('Random alignment mean score: ' + float_to_str(rand_mean, 2))
            print('         standard deviation: ' + float_to_str(rand_std_dev, 2, rand_mean))
            print()
            print('Low score threshold = ' + float_to_str(rand_mean, 2) + ' + ' + \
                  str(std_devs_over_mean) + ' x ' +  float_to_str(rand_std_dev, 2) + ' = ' + \
                  float_to_str(low_score_threshold, 2))
            print()

    reference_dict = {x.name: x for x in references}

    # Create the SAM file.
    if sam_filename:
        sam_file = open(sam_filename, 'w')

        # Header line.
        sam_file.write('@HD' + '\t')
        sam_file.write('VN:1.5' + '\t')
        sam_file.write('SO:unknown' + '\n')

        # Reference lines.
        for ref in references:
            sam_file.write('@SQ' + '\t')
            sam_file.write('SN:' + ref.name + '\t')
            sam_file.write('LN:' + str(ref.get_length()) + '\n')

        # Program line.
        sam_file.write('@PG' + '\t')
        sam_file.write('ID:' + 'ALIGNER_NAME')
        if full_command:
            sam_file.write('\tCL:' + full_command + '\t')
        sam_file.write('SC:' + str(scoring_scheme) + '\n')
        sam_file.close()

    # GraphMap can be used as a first-pass aligner. This has the advantage of saving time (GraphMap
    # is probably faster than the Seqan alignment) and it gives a good initial expected slope.
    if use_graphmap:

        # Make the temp directory, if necessary.
        temp_dir_exist_at_start = os.path.exists(temp_dir)
        if not temp_dir_exist_at_start:
            os.makedirs(temp_dir)

        # Run GraphMap and load in the resulting SAM.
        graphmap_sam = os.path.join(temp_dir, 'graphmap_alignments.sam')
        run_graphmap(ref_fasta, reads_fastq, graphmap_sam, graphmap_path, threads, scoring_scheme)
        graphmap_alignments = load_sam_alignments(graphmap_sam, read_dict, reference_dict,
                                                  scoring_scheme)
        # Clean up files and directories.
        if use_graphmap:
            os.remove(graphmap_sam)
            if not temp_dir_exist_at_start:
                os.rmdir(temp_dir)

        if VERBOSITY > 3 and graphmap_alignments:
            print('GraphMap alignments before extension')
            print('------------------------------------')
            for alignment in graphmap_alignments:
                print(alignment)
                if VERBOSITY > 4:
                    print(alignment.cigar)
            print()

        # Use Seqan to extend the GraphMap alignments so they are fully semi-global. In this
        # process, some alignments will be discarded (those too far from being semi-global).
        semi_global_graphmap_alignments = extend_to_semi_global(graphmap_alignments, scoring_scheme)
        if VERBOSITY > 3 and semi_global_graphmap_alignments:
            print('GraphMap alignments after extension')
            print('-----------------------------------')
            for alignment in semi_global_graphmap_alignments:
                print(alignment)
                if VERBOSITY > 4:
                    print(alignment.cigar)
            print()

        # Gather some statistics about the alignments.
        percent_ids = [x.percent_identity for x in graphmap_alignments]
        scores = [x.scaled_score for x in graphmap_alignments]
        percent_id_mean, percent_id_std_dev = get_mean_and_st_dev(percent_ids)
        score_mean, score_std_dev = get_mean_and_st_dev(scores)

        # Give the alignments to their corresponding reads.
        for alignment in semi_global_graphmap_alignments:
            read_dict[alignment.read.name].alignments.append(alignment)

        # We can now sort our reads into two different categories for further action:
        #   1) Reads with a single, high quality alignment in the middle of a reference. These reads
        #      are done!
        #   2) Reads that are incompletely (or not at all) aligned, have overlapping alignments or
        #      low quality alignments. These reads will be aligned again using Seqan.
        completed_reads = []
        reads_to_align = []
        for read_name in read_names:
            read = read_dict[read_name]
            update_expected_slope(read, low_score_threshold)
            if read.needs_seqan_realignment(low_score_threshold):
                reads_to_align.append(read)
            else:
                completed_reads.append(read)

        if VERBOSITY > 0:
            print_graphmap_summary_table(graphmap_alignments,
                                         percent_id_mean, percent_id_std_dev,
                                         score_mean, score_std_dev)

        # Write GraphMap alignments to SAM.
        if sam_filename:
            sam_file = open(sam_filename, 'a')
            for read in completed_reads:
                for alignment in read.alignments:
                    sam_file.write(alignment.get_sam_line())
            sam_file.close()

        # OPTIONAL TO DO: for reads which are completed, I could still try to refine GraphMap
        #                 alignments using my Seqan code. I'm not sure if it's worth it, so I
        #                 should give it a try to see what kind of difference it makes.

    # If we aren't using GraphMap as a first pass, then we align every read using Seqan.
    else:
        reads_to_align = [read_dict[x] for x in read_names]

    if VERBOSITY > 0:
        if VERBOSITY < 3:
            print('Aligning reads')
            print('--------------')
        num_realignments = len(reads_to_align)
        max_v = len(read_dict)
        if use_graphmap:
            print('Reads completed by GraphMap:', int_to_str(len(completed_reads), max_v))
            print('Reads to be realigned:      ', int_to_str(num_realignments, max_v))
            print()
    if VERBOSITY == 1:
        print_progress_line(0, num_realignments, prefix='Read: ')
    completed_count = 0

    # Create a C++ KmerPositions object and add each reference sequence.
    kmer_positions_ptr = new_kmer_positions()
    for ref in references:
        add_kmer_positions(kmer_positions_ptr, ref.name, ref.sequence, kmer_size)

    # If single-threaded, just do the work in a simple loop.
    if threads == 1:
        for read in reads_to_align:
            output = seqan_alignment(read, reference_dict, scoring_scheme, kmer_positions_ptr,
                                     low_score_threshold, keep_bad, kmer_size, min_align_length,
                                     sam_filename, allowed_overlap)
            completed_count += 1
            if VERBOSITY == 1:
                print_progress_line(completed_count, num_realignments, prefix='Read: ')
            if VERBOSITY > 1:
                print(output, end='')

    # If multi-threaded, use a thread pool.
    else:
        pool = ThreadPool(threads)
        arg_list = []
        for read in reads_to_align:
            arg_list.append((read, reference_dict, scoring_scheme, kmer_positions_ptr,
                             low_score_threshold, keep_bad, kmer_size, min_align_length,
                             sam_filename, allowed_overlap))
        for output in pool.imap(seqan_alignment_one_arg, arg_list, 1):
            completed_count += 1
            if VERBOSITY == 1:
                print_progress_line(completed_count, num_realignments, prefix='Read: ')
            if VERBOSITY > 1:
                print(output, end='')

    # We're done with the C++ KmerPositions object, so delete it now.
    delete_all_kmer_positions(kmer_positions_ptr)
    
    if VERBOSITY == 1:
        print('\n')

    # Output a summary of the reads' alignments.
    fully_aligned, partially_aligned, unaligned = group_reads_by_fraction_aligned(read_dict)
    ref_bases_aligned = 0
    for read in read_dict.itervalues():
        ref_bases_aligned += read.get_reference_bases_aligned()
    if VERBOSITY > 0:
        print('Read alignment summary')
        print('----------------------')
        max_v = max(len(read_dict), ref_bases_aligned)
        print('Total read count:       ', int_to_str(len(read_dict), max_v))
        print('Fully aligned reads:    ', int_to_str(len(fully_aligned), max_v))
        print('Partially aligned reads:', int_to_str(len(partially_aligned), max_v))
        if VERBOSITY > 2 and partially_aligned:
            print('    ' + ', '.join([x.name for x in partially_aligned]))
        print('Unaligned reads:        ', int_to_str(len(unaligned), max_v))
        if VERBOSITY > 2 and unaligned:
            print('    ' + ', '.join([x.name for x in unaligned]))
        print('Total bases aligned:    ', int_to_str(ref_bases_aligned, max_v) + ' bp')
        print()

    return read_dict

def print_graphmap_summary_table(graphmap_alignments, percent_id_mean, percent_id_std_dev,
                                 score_mean, score_std_dev):
    '''
    Prints a small table showing some details about the GraphMap alignments.
    '''
    print('Graphmap alignment summary')
    print('--------------------------')
    print('Total alignments:', int_to_str(len(graphmap_alignments)))
    print()

    table_lines = ['',
                   'Identity:',
                   'Score:']

    pad_length = max([len(x) for x in table_lines]) + 2
    table_lines = [x.ljust(pad_length) for x in table_lines]

    table_lines[0] += 'Mean'
    table_lines[1] += float_to_str(percent_id_mean, 2)
    if percent_id_mean:
        table_lines[1] += '%'
    table_lines[2] += float_to_str(score_mean, 2)

    pad_length = max([len(x) for x in table_lines]) + 2
    table_lines = [x.ljust(pad_length) for x in table_lines]

    table_lines[0] += 'Std dev'
    table_lines[1] += float_to_str(percent_id_std_dev, 2)
    if percent_id_std_dev:
        table_lines[1] += '%'
    table_lines[2] += float_to_str(score_std_dev, 2)

    for line in table_lines:
        print(line)
    print()
    print('Current mean reference length / read length:', float_to_str(EXPECTED_SLOPE, 5))
    print()

def extend_to_semi_global(alignments, scoring_scheme):
    '''
    This function returns truly semi-global alignments made from the input alignments.
    '''
    if VERBOSITY > 3 and alignments:
        print('Extending alignments')
        print('-------------------')

    allowed_missing_bases = 100 # TO DO: MAKE THIS A PARAMETER
    semi_global_alignments = []
    for alignment in alignments:
        total_missing_bases = alignment.get_total_missing_bases()

        # If an input alignment is already semi-global, then it's included in the output.
        if total_missing_bases == 0:
            semi_global_alignments.append(alignment)

        # If an input alignment is almost semi-global (below a threshold), and not too close to the
        # end of the reference, then it is extended to make it semi-global.
        elif total_missing_bases <= allowed_missing_bases:
            missing_start = alignment.get_missing_bases_at_start()
            missing_end = alignment.get_missing_bases_at_end()
            if missing_start and alignment.ref_start_pos >= 2 * missing_start:
                alignment.extend_start(scoring_scheme)
            if missing_end and alignment.ref_end_gap >= 2 * missing_end:
                alignment.extend_end(scoring_scheme)
            semi_global_alignments.append(alignment)

        # If an input alignment is above the threshold (not close to being semi-global), it is
        # discarded.
        else:
            pass

    return semi_global_alignments

def load_references(fasta_filename, verbosity):
    '''
    This function loads in sequences from a FASTA file and returns a list of Reference objects.
    '''
    references = []
    total_bases = 0
    if verbosity > 0:
        print()
        print('Loading references')
        print('------------------')
        num_refs = sum(1 for line in open(fasta_filename) if line.startswith('>'))
        if not num_refs:
            quit_with_error('There are no references sequences in ' + fasta_filename)
        print_progress_line(0, num_refs)
    fasta_file = open(fasta_filename, 'r')
    name = ''
    sequence = ''
    last_progress = 0.0
    for line in fasta_file:
        line = line.strip()
        if not line:
            continue
        if line.startswith('>'): # Header line = start of new contig
            if name:
                references.append(Reference(name, sequence))
                total_bases += len(sequence)
                if verbosity > 0:
                    progress = 100.0 * len(references) / num_refs
                    progress_rounded_down = float(int(progress))
                    if progress_rounded_down > last_progress:
                        print_progress_line(len(references), num_refs, total_bases)
                        last_progress = progress_rounded_down
                name = ''
                sequence = ''
            name = get_nice_header(line[1:])
        else:
            sequence += line
    fasta_file.close()
    if name:
        references.append(Reference(name, sequence))
        total_bases += len(sequence)
        if verbosity > 0:
            print_progress_line(len(references), num_refs, total_bases)
            print('\n')
    return references


def load_long_reads(fastq_filename, verbosity):
    '''
    This function loads in long reads from a FASTQ file and returns a dictionary where key = read
    name and value = Read object. It also returns a list of read names, in the order they are in
    the file.
    '''
    read_dict = {}
    read_names = []
    total_bases = 0
    last_progress = 0.0
    if verbosity > 0:
        print('Loading reads')
        print('-------------')
        num_reads = sum(1 for line in open(fastq_filename)) // 4
        print_progress_line(0, num_reads)
    fastq = open(fastq_filename, 'r')
    for line in fastq:
        name = line.strip()[1:].split()[0]
        sequence = next(fastq).strip()
        _ = next(fastq)
        qualities = next(fastq).strip()
        read_dict[name] = Read(name, sequence, qualities)
        read_names.append(name)
        total_bases += len(sequence)
        if verbosity > 0:
            progress = 100.0 * len(read_dict) / num_reads
            progress_rounded_down = float(int(progress))
            if progress_rounded_down > last_progress:
                print_progress_line(len(read_dict), num_reads, total_bases)
                last_progress = progress_rounded_down    
    fastq.close()
    if verbosity > 0:
        print('\n')
    return read_dict, read_names

def run_graphmap(fasta, long_reads_fastq, sam_file, graphmap_path, threads, scoring_scheme):
    '''
    This function runs GraphMap for the given inputs and produces a SAM file at the given location.
    '''
    graphmap_version = get_graphmap_version(graphmap_path)

    # Build the GraphMap command. There is a bit of difference if we're using a version before or
    # after v0.3.0.
    command = [graphmap_path]
    if graphmap_version >= 0.3:
        command.append('align')
    command += ['-r', fasta,
                '-d', long_reads_fastq,
                '-o', sam_file,
                '-t', str(threads),
                '-a', 'anchorgotoh']
    command += scoring_scheme.get_graphmap_parameters()

    if VERBOSITY > 0:
        print('Aligning with GraphMap')
        print('----------------------')
        print(' '.join(command))

    # Print the GraphMap output as it comes. I gather up and display lines so I can display fewer
    # progress lines. This helps when piping the output to file (otherwise the output can be
    # excessively large).
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = ''
    last_progress = -1.0
    read_progress_started = False
    read_progress_finished = False
    while process.poll() is None:
        graphmap_output = process.stderr.read(1)
        if VERBOSITY > 0:
            line += graphmap_output
            if line.endswith('\n') or line.endswith('\r'):
                if line.strip():
                    if 'CPU time' in line:
                        read_progress_started = True
                        trimmed_line = line.strip().split('] ')[2].split(', l')[0]
                        progress = float(trimmed_line.split('(')[1].split(')')[0][:-1])
                        progress_rounded_down = float(int(progress))
                        if progress_rounded_down > last_progress:
                            print('\r' + trimmed_line, end='')
                            last_progress = progress_rounded_down
                    elif VERBOSITY > 1:
                        if read_progress_started and not read_progress_finished:
                            print()
                            read_progress_finished = True
                        print(line, end='')
                line = ''
    if VERBOSITY == 1:
        print('\n')
    if VERBOSITY > 1:
        print()

    # Clean up.
    if os.path.isfile(fasta + '.gmidx'):
        os.remove(fasta + '.gmidx')
    if os.path.isfile(fasta + '.gmidxsec'):
        os.remove(fasta + '.gmidxsec')

    if not os.path.isfile(sam_file):
        quit_with_error('GraphMap failure')

def get_graphmap_version(graphmap_path):
    '''
    Returns the version of GraphMap.
    '''
    command = [graphmap_path, '-h']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    allout = out + err
    if 'Version: v' not in allout:
        command = [graphmap_path, 'align', '-h']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        allout = out + err
    if 'Version: v' not in allout:
        return 0.0
    version_i = allout.find('Version: v')
    version = allout[version_i + 10:]
    version = version.split()[0]
    version = '.'.join(version.split('.')[0:2])
    return float(version)

def load_sam_alignments(sam_filename, read_dict, reference_dict, scoring_scheme):
    '''
    This function returns a list of Alignment objects from the given SAM file.
    '''
    sam_alignments = []
    sam_file = open(sam_filename, 'r')
    for line in sam_file:
        line = line.strip()
        if line and not line.startswith('@') and line.split('\t', 3)[2] != '*':
            sam_alignments.append(Alignment(sam_line=line, read_dict=read_dict,
                                            reference_dict=reference_dict,
                                            scoring_scheme=scoring_scheme))
    return sam_alignments

def seqan_alignment_one_arg(all_args):
    '''
    This is just a one-argument version of seqan_alignment to make it easier to use that function
    in a thread pool.
    '''
    read, reference_dict, scoring_scheme, kmer_positions_ptr, low_score_threshold, keep_bad, \
        kmer_size, min_align_length, sam_filename, allowed_overlap = all_args
    return seqan_alignment(read, reference_dict, scoring_scheme, kmer_positions_ptr,
                           low_score_threshold, keep_bad, kmer_size, min_align_length,
                           sam_filename, allowed_overlap)

def seqan_alignment(read, reference_dict, scoring_scheme, kmer_positions_ptr, low_score_threshold,
                    keep_bad, kmer_size, min_align_length, sam_filename, allowed_overlap):
    '''
    Aligns a single read against all reference sequences using Seqan.
    '''
    start_time = time.time()
    output = ''
    if VERBOSITY > 1:
        output += str(read) + '\n'
    if VERBOSITY > 2:
        output += '-' * len(str(read)) + '\n'

    starting_graphmap_alignments = len(read.alignments)
    if VERBOSITY > 2:
        output += 'Graphmap alignments:\n'
        if starting_graphmap_alignments:
            for alignment in read.alignments:
                output += '  ' + str(alignment) + '\n'
        else:
            output += '  None\n'

    # print(read, EXPECTED_SLOPE) # TEMP
    # sys.stdout.flush() # TEMP

    results = semi_global_alignment(read.name, read.sequence, VERBOSITY,
                                    EXPECTED_SLOPE, kmer_positions_ptr,
                                    scoring_scheme.match, scoring_scheme.mismatch,
                                    scoring_scheme.gap_open, scoring_scheme.gap_extend,
                                    low_score_threshold, keep_bad, kmer_size).split(';')
    alignment_strings = results[:-1]
    output += results[-1]

    for alignment_string in alignment_strings:
        alignment = Alignment(seqan_output=alignment_string, read=read,
                              reference_dict=reference_dict,
                              scoring_scheme=scoring_scheme)
        read.alignments.append(alignment)

    if VERBOSITY > 2:
        if not alignment_strings:
            output += '  None\n'
        else:
            output += 'All Seqan alignments (time to align = ' + \
                      float_to_str(time.time() - start_time, 3) + ' s):\n'
            for alignment in read.alignments:
                if alignment.alignment_type != 'SAM':
                    output += '  ' + str(alignment) + '\n'
                    if VERBOSITY > 3:
                        output += alignment.cigar + '\n'

    read.remove_conflicting_alignments(allowed_overlap)
    if not keep_bad:
        read.remove_low_score_alignments(low_score_threshold)
    read.remove_short_alignments(min_align_length)

    if VERBOSITY > 2:
        output += 'Final alignments:\n'
    if VERBOSITY > 1:
        if read.alignments:
            for alignment in read.alignments:
                output += '  ' + str(alignment) + '\n'
        else:
            output += '  None\n'
        output += '\n'

    # Write alignments to SAM.
    if sam_filename and read.alignments:
        sam_write_lock.acquire()
        sam_file = open(sam_filename, 'a')
        for alignment in read.alignments:
            sam_file.write(alignment.get_sam_line())
        sam_file.close()
        sam_write_lock.release()

    update_expected_slope(read, low_score_threshold)
    return output

def get_ref_shift_from_cigar_part(cigar_part):
    '''
    This function returns how much a given cigar moves on a reference.
    Examples:
      * '5M' returns 5
      * '5S' returns 0
      * '5D' returns 5
      * '5I' returns 0
    '''
    if cigar_part[-1] == 'M':
        return int(cigar_part[:-1])
    if cigar_part[-1] == 'I':
        return 0
    if cigar_part[-1] == 'D':
        return int(cigar_part[:-1])
    if cigar_part[-1] == 'S':
        return 0

def simplify_ranges(ranges):
    '''
    Collapses overlapping ranges together. Input ranges are tuples of (start, end) in the normal
    Python manner where the end isn't included.
    '''
    fixed_ranges = []
    for int_range in ranges:
        if int_range[0] > int_range[1]:
            fixed_ranges.append((int_range[1], int_range[0]))
        elif int_range[0] < int_range[1]:
            fixed_ranges.append(int_range)
    starts_ends = [(x[0], 1) for x in fixed_ranges]
    starts_ends += [(x[1], -1) for x in fixed_ranges]
    starts_ends.sort(key=lambda x: x[0])
    current_sum = 0
    cumulative_sum = []
    for start_end in starts_ends:
        current_sum += start_end[1]
        cumulative_sum.append((start_end[0], current_sum))
    prev_depth = 0
    start = 0
    combined = []
    for pos, depth in cumulative_sum:
        if prev_depth == 0:
            start = pos
        elif depth == 0:
            combined.append((start, pos))
        prev_depth = depth
    return combined

def range_is_contained(test_range, other_ranges):
    '''
    Returns True if test_range is entirely contained within any range in other_ranges.
    '''
    start, end = test_range
    for other_range in other_ranges:
        if other_range[0] <= start and other_range[1] >= end:
            return True
    return False

def range_overlap(test_range, other_ranges):
    '''
    Returns the size of the overlap (integer) between the two ranges.
    '''
    start, end = test_range
    max_overlap = 0
    for other_range in other_ranges:
        max_overlap = max(max_overlap, min(end, other_range[1]) - max(start, other_range[0]))
    return max_overlap

def is_header_spades_format(contig_name):
    '''
    Returns whether or not the header appears to be in the SPAdes/Velvet format.
    Example: NODE_5_length_150905_cov_4.42519
    '''
    contig_name_parts = contig_name.split('_')
    return len(contig_name_parts) > 5 and \
           (contig_name_parts[0] == 'NODE' or contig_name_parts[0] == 'EDGE') and \
           contig_name_parts[2] == 'length' and contig_name_parts[4] == 'cov'

def get_nice_header(header):
    '''
    For a header with a SPAdes/Velvet format, this function returns a simplified string that is
    just NODE_XX where XX is the contig number.
    For any other format, this function trims off everything following the first whitespace.
    '''
    if is_header_spades_format(header):
        return 'NODE_' + header.split('_')[1]
    else:
        return header.split()[0]

def reverse_complement(seq):
    '''Given a DNA sequences, this function returns the reverse complement sequence.'''
    return ''.join([complement_base(seq[i]) for i in xrange(len(seq) - 1, -1, -1)])

def complement_base(base):
    '''Given a DNA base, this returns the complement.'''
    if base == 'A':
        return 'T'
    if base == 'T':
        return 'A'
    if base == 'G':
        return 'C'
    if base == 'C':
        return 'G'
    if base == 'a':
        return 't'
    if base == 't':
        return 'a'
    if base == 'g':
        return 'c'
    if base == 'c':
        return 'g'
    forward = 'RYSWKMryswkmBDHVbdhvNn.-?'
    reverse = 'YRSWMKyrswmkVHDBvhdbNn.-?N'
    return reverse[forward.find(base)]

def get_mean_and_st_dev(num_list):
    '''
    This function returns the mean and standard deviation of the given list of numbers.
    '''
    num = len(num_list)
    if num == 0:
        return None, None
    mean = sum(num_list) / num
    if num == 1:
        return mean, None
    sum_squares = sum((x - mean) ** 2 for x in num_list)
    st_dev = (sum_squares / (num - 1)) ** 0.5
    return mean, st_dev

def print_progress_line(completed, total, base_pairs=None, prefix=None):
    '''
    Prints a progress line to the screen using a carriage return to overwrite the previous progress
    line.
    '''
    progress_str = ''
    if prefix:
        progress_str += prefix
    progress_str += int_to_str(completed) + ' / ' + int_to_str(total)
    progress_str += ' (' + '%.1f' % (100.0 * completed / total) + '%)'
    if base_pairs is not None:
        progress_str += ' - ' + int_to_str(base_pairs) + ' bp'
    print('\r' + progress_str, end='')
    sys.stdout.flush()

def group_reads_by_fraction_aligned(read_dict):
    '''
    Groups reads into three lists:
      1) Fully aligned
      2) Partially aligned
      3) Unaligned
    '''
    fully_aligned_reads = []
    partially_aligned_reads = []
    unaligned_reads = []
    for read in read_dict.itervalues():
        fraction_aligned = read.get_fraction_aligned()
        if fraction_aligned == 1.0:
            fully_aligned_reads.append(read)
        elif fraction_aligned == 0.0:
            unaligned_reads.append(read)
        else:
            partially_aligned_reads.append(read)
    return fully_aligned_reads, partially_aligned_reads, unaligned_reads

def get_auto_score_threshold(scoring_scheme, std_devs_over_mean):
    '''
    This function determines a good low score threshold for the alignments. To do this it examines
    the distribution of scores acquired by aligning random sequences.
    '''
    # TO DO: make the random alignments run in separate threads to be a bit faster
    mean, std_dev = get_random_sequence_alignment_mean_and_std_dev(100, 10000, scoring_scheme)
    threshold = mean + (std_devs_over_mean * std_dev)

    # Keep the threshold bounded to sane levels.
    threshold = min(threshold, 95.0)
    threshold = max(threshold, 50.0)
    return threshold, mean, std_dev


def update_expected_slope(read, low_score_threshold):
    '''
    This function updates the EXPECTED_SLOPE and ALIGNMENTS_CONTRIBUTING_TO_EXPECTED_SLOPE global
    variables using a read, but only if the read's alignment looks good.
    '''
    if len(read.alignments) == 1 and \
       read.alignments[0].read_start_pos == 0 and read.alignments[0].read_end_gap == 0 and \
       read.alignments[0].scaled_score > low_score_threshold:
        global EXPECTED_SLOPE
        global TOTAL_REF_LENGTH
        global TOTAL_READ_LENGTH
        TOTAL_REF_LENGTH += read.alignments[0].get_aligned_ref_length()
        TOTAL_READ_LENGTH += read.alignments[0].get_aligned_read_length()
        EXPECTED_SLOPE = TOTAL_REF_LENGTH / TOTAL_READ_LENGTH



class AlignmentScoringScheme(object):
    '''
    This class holds an alignment scoring scheme.
    '''
    def __init__(self, scheme_string):
        scheme_parts = scheme_string.split(',')

        # Default scoring scheme
        self.match = 3
        self.mismatch = -6
        self.gap_open = -5
        self.gap_extend = -2

        if len(scheme_parts) == 4:
            self.match = int(scheme_parts[0])
            self.mismatch = int(scheme_parts[1])
            self.gap_open = int(scheme_parts[2])
            self.gap_extend = int(scheme_parts[3])

    def __repr__(self):
        return str(self.match) + ',' + str(self.mismatch) + ',' + str(self.gap_open) + ',' + \
               str(self.gap_extend)

    def get_full_string(self):
        return 'match = ' + str(self.match) + ', mismatch = ' + str(self.mismatch) + \
               ', gap open = ' + str(self.gap_open) + ', gap extend = ' + str(self.gap_extend)


    def get_graphmap_parameters(self):
        '''
        Returns the scoring scheme in the form of GraphMap parameters for subprocess.
        '''
        return ['-M', str(self.match),
                '-X', str(-self.mismatch),
                '-G', str(-self.gap_open),
                '-E', str(-self.gap_extend)]



class Reference(object):
    '''
    This class holds a reference sequence: just a name and a nucleotide sequence.
    '''
    def __init__(self, name, sequence):
        self.name = name
        self.sequence = sequence.upper()

    def get_length(self):
        '''
        Returns the sequence length.
        '''
        return len(self.sequence)



class Read(object):
    '''
    This class holds a long read, e.g. from PacBio or Oxford Nanopore.
    '''
    def __init__(self, name, sequence, qualities):
        self.name = name
        self.sequence = sequence.upper()
        self.qualities = qualities
        self.alignments = []

    def __repr__(self):
        return self.name + ' (' + str(len(self.sequence)) + ' bp)'

    def get_length(self):
        '''
        Returns the sequence length.
        '''
        return len(self.sequence)

    def needs_seqan_realignment(self, low_score_threshold):
        '''
        This function returns True or False based on whether a read was nicely aligned by GraphMap
        or needs to be realigned with Seqan.
        '''
        # Either zero or more than one alignments result in realignment.
        if len(self.alignments) != 1:
            return True

        # Overlapping alignments or low quality alignments result in realignment.
        only_alignment = self.alignments[0]
        return (not only_alignment.is_whole_read() or
                only_alignment.scaled_score < low_score_threshold)

    def needs_more_sensitive_alignment(self, low_score_threshold):
        '''
        This function returns False if for every part of the read there is at least one alignment
        that exceeds the score threshold.
        ''' 
        read_ranges = [x.read_start_end_positive_strand() \
                       for x in self.alignments if x.scaled_score >= low_score_threshold]
        read_ranges = simplify_ranges(read_ranges)
        well_aligned_length = sum([x[1] - x[0] for x in read_ranges])
        return well_aligned_length < len(self.sequence)

    def remove_conflicting_alignments(self, allowed_overlap):
        '''
        This function removes alignments from the read which are likely to be spurious or
        redundant.
        '''
        self.alignments = sorted(self.alignments, reverse=True,
                                 key=lambda x: (x.raw_score, random.random()))
        kept_alignments = []
        kept_alignment_ranges = []
        for alignment in self.alignments:
            this_range = alignment.read_start_end_positive_strand()

            # Don't keep alignments for which their part of the read is already aligned.
            if range_is_contained(this_range, kept_alignment_ranges):
                continue

            # Don't keep alignments which overlap too much with existing alignments.
            if range_overlap(this_range, kept_alignment_ranges) > allowed_overlap:
                continue

            # Don't keep alignments that seem to be very similar to an already kept alignment.
            keep_alignment = True
            for kept_alignment in kept_alignments:
                if kept_alignment.is_very_similar(alignment):
                    keep_alignment = False
                    break

            if keep_alignment:
                kept_alignments.append(alignment)
                kept_alignment_ranges = simplify_ranges(kept_alignment_ranges + [this_range])

        kept_alignments = sorted(kept_alignments,
                                 key=lambda x: x.read_start_end_positive_strand()[0])
        self.alignments = kept_alignments

    def remove_low_score_alignments(self, low_score_threshold):
        '''
        This function removes alignments with identity below the cutoff.
        '''
        self.alignments = [x for x in self.alignments if x.scaled_score >= low_score_threshold]

    def remove_short_alignments(self, min_align_length):
        '''
        This function removes alignments with identity below the cutoff.
        '''
        self.alignments = [x for x in self.alignments \
                           if x.get_aligned_ref_length() >= min_align_length]


    def get_fastq(self):
        '''
        Returns a string for the read in FASTQ format. It contains four lines and ends in a line
        break.
        '''
        return '@' + self.name + '\n' + \
               self.sequence + '\n' + \
               '+' + self.name + '\n' + \
               self.qualities + '\n'

    def get_fasta(self):
        '''
        Returns a string for the read in FASTA format. It contains two lines and ends in a line
        break.
        '''
        return '>' + self.name + '\n' + \
               self.sequence + '\n'

    def get_descriptive_string(self):
        '''
        Returns a multi-line string that describes the read and its alignments.
        '''
        header = self.name + ' (' + str(len(self.sequence)) + ' bp)'
        line = '-' * len(header)
        description = header + '\n' + line + '\n'
        if not self.alignments:
            description += 'no alignments'
        else:
            description += '%.2f' % (100.0 * self.get_fraction_aligned()) + '% aligned\n'
            description += '\n'.join([str(x) for x in self.alignments])
        return description + '\n\n'

    def get_fraction_aligned(self):
        '''
        This function returns the fraction of the read which is covered by any of the read's
        alignments.
        '''
        read_ranges = [x.read_start_end_positive_strand() \
                       for x in self.alignments]
        read_ranges = simplify_ranges(read_ranges)
        aligned_length = sum([x[1] - x[0] for x in read_ranges])
        return aligned_length / len(self.sequence)

    def get_reference_bases_aligned(self):
        '''
        This function returns the number of bases aligned with respect to the reference.
        '''
        return sum([x.get_aligned_ref_length() for x in self.alignments])




class Alignment(object):
    '''
    This class describes an alignment between a long read and a contig.
    It can be constructed either from a SAM line made by GraphMap or from the C++ Seqan output.
    '''
    def __init__(self,
                 sam_line=None, read_dict=None,
                 seqan_output=None, read=None,
                 reference_dict=None, scoring_scheme=None):

        # Make sure we have the appropriate inputs for one of the two ways to construct an
        # alignment.
        assert (sam_line and read_dict) or (seqan_output and read)

        # Some inputs are required for both types of construction.
        assert scoring_scheme and reference_dict

        # Read details
        self.read = None
        self.read_start_pos = None
        self.read_end_pos = None
        self.read_end_gap = None

        # Reference details
        self.ref = None
        self.ref_start_pos = None
        self.ref_end_pos = None
        self.ref_end_gap = None

        # Alignment details
        self.alignment_type = None
        self.rev_comp = None
        self.cigar = None
        self.cigar_parts = None
        self.match_count = None
        self.mismatch_count = None
        self.insertion_count = None
        self.deletion_count = None
        self.alignment_length = None
        self.edit_distance = None
        self.percent_identity = None
        self.raw_score = None
        self.scaled_score = None
        self.milliseconds = None

        # How some of the values are gotten depends on whether this alignment came from a GraphMap
        # SAM or a Seqan alignment.
        if seqan_output:
            self.setup_using_seqan_output(seqan_output, read, reference_dict)
        elif sam_line:
            self.setup_using_sam(sam_line, read_dict, reference_dict)

        self.tally_up_score_and_errors(scoring_scheme)

    def setup_using_seqan_output(self, seqan_output, read, reference_dict):
        '''
        This function sets up the Alignment using the Seqan results. This kind of alignment has
        complete details about the alignment.
        '''
        self.alignment_type = 'Seqan'
        seqan_parts = seqan_output.split(',', 7)
        assert len(seqan_parts) >= 8

        self.rev_comp = (seqan_parts[1] == '-')
        self.cigar = seqan_parts[7]
        self.cigar_parts = re.findall(r'\d+\w', self.cigar)
        self.milliseconds = int(seqan_parts[6])

        self.read = read
        self.read_start_pos = int(seqan_parts[2])
        self.read_end_pos = int(seqan_parts[3])
        self.read_end_gap = self.read.get_length() - self.read_end_pos

        self.ref = reference_dict[get_nice_header(seqan_parts[0])]
        self.ref_start_pos = int(seqan_parts[4])
        self.ref_end_pos = int(seqan_parts[5])
        self.ref_end_gap = len(self.ref.sequence) - self.ref_end_pos

    def setup_using_sam(self, sam_line, read_dict, reference_dict):
        '''
        This function sets up the Alignment using a SAM line.
        '''
        self.alignment_type = 'SAM'
        sam_parts = sam_line.split('\t')
        self.rev_comp = bool(int(sam_parts[1]) & 0x10)
        self.cigar = sam_parts[5]
        self.cigar_parts = re.findall(r'\d+\w', self.cigar)

        self.read = read_dict[sam_parts[0]]
        self.read_start_pos = self.get_start_soft_clips()
        self.read_end_pos = self.read.get_length() - self.get_end_soft_clips()
        self.read_end_gap = self.get_end_soft_clips()

        self.ref = reference_dict[get_nice_header(sam_parts[2])]
        self.ref_start_pos = int(sam_parts[3]) - 1
        self.ref_end_pos = self.ref_start_pos
        for cigar_part in self.cigar_parts:
            self.ref_end_pos += get_ref_shift_from_cigar_part(cigar_part)

        # If all is good with the CIGAR, then we should never end up with a ref_end_pos out of the
        # reference range. But a CIGAR error (which has occurred in GraphMap) can cause this, so
        # check here.
        if self.ref_end_pos > len(self.ref.sequence):
            self.ref_end_pos = len(self.ref.sequence)

        self.ref_end_gap = len(self.ref.sequence) - self.ref_end_pos

    def tally_up_score_and_errors(self, scoring_scheme):
        '''
        This function steps through the CIGAR string for the alignment to get the score, identity
        and count/locations of errors.
        '''
        # Clear any existing tallies.
        self.match_count = 0
        self.mismatch_count = 0
        self.insertion_count = 0
        self.deletion_count = 0
        self.percent_identity = 0.0
        self.raw_score = 0

        # Remove the soft clipping parts of the CIGAR string for tallying.
        cigar_parts = self.cigar_parts[:]
        if cigar_parts[0][-1] == 'S':
            cigar_parts.pop(0)
        if cigar_parts and cigar_parts[-1][-1] == 'S':
            cigar_parts.pop()
        if not cigar_parts:
            return

        read_len = self.read.get_length()
        if self.rev_comp:
            read_seq = reverse_complement(self.read.sequence)
        else:
            read_seq = self.read.sequence
        read_i = self.read_start_pos

        ref_len = self.ref.get_length()
        ref_seq = self.ref.sequence
        ref_i = self.ref_start_pos
        align_i = 0

        for cigar_part in cigar_parts:
            cigar_count = int(cigar_part[:-1])
            cigar_type = cigar_part[-1]
            if cigar_type == 'I' or cigar_type == 'D':
                cigar_score = scoring_scheme.gap_open + \
                              ((cigar_count - 1) * scoring_scheme.gap_extend)
            if cigar_type == 'I':
                self.insertion_count += cigar_count
                read_i += cigar_count
            elif cigar_type == 'D':
                self.deletion_count += cigar_count
                ref_i += cigar_count
            else: # match/mismatch
                cigar_score = 0
                for _ in xrange(cigar_count):
                    # If all is good with the CIGAR, then we should never end up with a sequence
                    # index out of the sequence range. But a CIGAR error (which has occurred in
                    # GraphMap) can cause this, so check here.
                    if read_i >= read_len or ref_i >= ref_len:
                        break
                    read_base = read_seq[read_i]
                    ref_base = ref_seq[ref_i]
                    if read_base == ref_base:
                        self.match_count += 1
                        cigar_score += scoring_scheme.match
                    else:
                        self.mismatch_count += 1
                        cigar_score += scoring_scheme.mismatch
                    read_i += 1
                    ref_i += 1

            self.raw_score += cigar_score
            align_i += cigar_count

        self.percent_identity = 100.0 * self.match_count / align_i
        self.edit_distance = self.mismatch_count + self.insertion_count + self.deletion_count
        self.alignment_length = align_i
        perfect_score = scoring_scheme.match * self.alignment_length
        worst_score = scoring_scheme.mismatch * self.alignment_length
        self.scaled_score = 100.0 * (self.raw_score - worst_score) / (perfect_score - worst_score)

    def extend_start(self, scoring_scheme):
        '''
        This function extends the start of the alignment to remove any missing start bases.
        '''
        if VERBOSITY > 3:
            print(self)
            if len(self.cigar) > 20:
                print('   ', self.cigar[:20] + '...')
            else:
                print('   ', self.cigar[:20])
            cigar_length_before = len(self.cigar)

        # We will try the start extension a few times, if necessary, with increasing margin sizes.
        # The first try should usually be sufficient.
        for i in range(3):
            margin_size = 2**(i+1) # 2, 4, 8
            missing_start_bases = self.get_missing_bases_at_start()
            realigned_bases = margin_size * missing_start_bases
            realigned_read_end = self.read_start_pos
            realigned_read_start = max(0, realigned_read_end - realigned_bases)
            realigned_ref_end = self.ref_start_pos
            realigned_ref_start = max(0, realigned_ref_end - realigned_bases)
            if self.rev_comp:
                realigned_read_seq = \
                    reverse_complement(self.read.sequence)[realigned_read_start:realigned_read_end]
            else:
                realigned_read_seq = self.read.sequence[realigned_read_start:realigned_read_end]
            realigned_ref_seq = self.ref.sequence[realigned_ref_start:realigned_ref_end]
            assert len(realigned_ref_seq) >= len(realigned_read_seq)


            # Call the C++ function to do the actual alignment.
            alignment_result = start_extension_alignment(realigned_read_seq, realigned_ref_seq,
                                                         scoring_scheme)
            seqan_parts = alignment_result.split(',', 7)
            assert len(seqan_parts) >= 8

            # If the extended alignment has taken us far enough (should usually be the case), then
            # use it. In rare cases, the margin size won't have been enough, so we try again with a
            # bigger margin.
            if int(seqan_parts[2]) == 0:
                break

        # Set the new read start.
        self.read_start_pos = int(seqan_parts[2])

        # Set the new reference start.
        self.ref_start_pos = realigned_ref_start + int(seqan_parts[4])

        # Replace the S part at the beginning the alignment's CIGAR with the CIGAR just made. If
        # the last part of the new CIGAR is of the same type as the first part of the existing
        # CIGAR, they will need to be merged.
        new_cigar_parts = re.findall(r'\d+\w', seqan_parts[7])
        old_cigar_parts = self.cigar_parts[1:]
        if new_cigar_parts[-1][-1] == old_cigar_parts[0][-1]:
            part_sum = int(new_cigar_parts[-1][:-1]) + int(old_cigar_parts[0][:-1])
            merged_part = str(part_sum) + new_cigar_parts[-1][-1]
            new_cigar_parts = new_cigar_parts[:-1] + [merged_part]
            old_cigar_parts = old_cigar_parts[1:]
        self.cigar_parts = new_cigar_parts + old_cigar_parts
        self.cigar = ''.join(self.cigar_parts)

        self.tally_up_score_and_errors(scoring_scheme)

        if VERBOSITY > 3:
            cigar_length_increase = len(self.cigar) - cigar_length_before
            cigar_size_to_print = 20 + cigar_length_increase
            print(self)
            if len(self.cigar) > cigar_size_to_print:
                print('    ', self.cigar[:cigar_size_to_print] + '...')
            else:
                print('    ', self.cigar[:cigar_size_to_print])
            print()

    def extend_end(self, scoring_scheme):
        '''
        This function extends the end of the alignment to remove any missing end bases.
        '''
        if VERBOSITY > 3:
            print(self)
            if len(self.cigar) > 20:
                print('    ...' + self.cigar[-20:])
            else:
                print('       ' + self.cigar[-20:])
            cigar_length_before = len(self.cigar)

        # We will try the end extension a few times, if necessary, with increasing margin sizes.
        # The first try should usually be sufficient.
        for i in range(3):
            margin_size = 2**(i+1) # 2, 4, 8
            missing_end_bases = self.get_missing_bases_at_end()
            realigned_bases = margin_size * missing_end_bases
            realigned_read_start = self.read_end_pos
            realigned_read_end = min(self.read.get_length(), realigned_read_start + realigned_bases)
            realigned_ref_start = self.ref_end_pos
            realigned_ref_end = min(len(self.ref.sequence), realigned_ref_start + realigned_bases)
            if self.rev_comp:
                realigned_read_seq = \
                    reverse_complement(self.read.sequence)[realigned_read_start:realigned_read_end]
            else:
                realigned_read_seq = self.read.sequence[realigned_read_start:realigned_read_end]
            realigned_ref_seq = self.ref.sequence[realigned_ref_start:realigned_ref_end]
            assert len(realigned_ref_seq) >= len(realigned_read_seq)

            # Call the C++ function to do the actual alignment.
            alignment_result = end_extension_alignment(realigned_read_seq, realigned_ref_seq,
                                                       scoring_scheme)
            seqan_parts = alignment_result.split(',', 7)
            assert len(seqan_parts) >= 8

            # If the extended alignment has taken us far enough (should usually be the case), then
            # use it. In rare cases, the margin size won't have been enough, so we try again with a
            # bigger margin.
            if self.read_end_pos + int(seqan_parts[3]) == self.read.get_length():
                break

        # Set the new read end.
        self.read_end_pos += int(seqan_parts[3])
        self.read_end_gap = self.read.get_length() - self.read_end_pos

        # Set the new reference end.
        self.ref_end_pos += int(seqan_parts[5])
        self.ref_end_gap = len(self.ref.sequence) - self.ref_end_pos

        # Replace the S part at the end the alignment's CIGAR with the CIGAR just made. If
        # the first part of the new CIGAR is of the same type as the last part of the existing
        # CIGAR, they will need to be merged.
        old_cigar_parts = self.cigar_parts[:-1]
        new_cigar_parts = re.findall(r'\d+\w', seqan_parts[7])
        if old_cigar_parts[-1][-1] == new_cigar_parts[0][-1]:
            part_sum = int(old_cigar_parts[-1][:-1]) + int(new_cigar_parts[0][:-1])
            merged_part = str(part_sum) + new_cigar_parts[0][-1]
            old_cigar_parts = old_cigar_parts[:-1] + [merged_part]
            new_cigar_parts = new_cigar_parts[1:]
        self.cigar_parts = old_cigar_parts + new_cigar_parts
        self.cigar = ''.join(self.cigar_parts)

        self.tally_up_score_and_errors(scoring_scheme)

        if VERBOSITY > 3:
            cigar_length_increase = len(self.cigar) - cigar_length_before
            cigar_size_to_print = 20 + cigar_length_increase
            print(self)
            if len(self.cigar) > cigar_size_to_print:
                print('    ...' + self.cigar[-cigar_size_to_print:])
            else:
                print('       ' + self.cigar[-cigar_size_to_print:])
            print()

    def __repr__(self):
        read_start, read_end = self.read_start_end_positive_strand()
        return_str = self.read.name + ' (' + str(read_start) + '-' + str(read_end) + ', '
        if self.rev_comp:
            return_str += 'strand: -), '
        else:
            return_str += 'strand: +), '
        return_str += self.ref.name + ' (' + str(self.ref_start_pos) + '-' + \
                      str(self.ref_end_pos) + ')'
        if self.scaled_score is not None:
            return_str += ', raw score = ' + str(self.raw_score)
            return_str += ', scaled score = ' + '%.2f' % self.scaled_score
        if self.percent_identity is not None:
            return_str += ', ' + '%.2f' % self.percent_identity + '% ID'
        return return_str

    def get_aligned_ref_length(self):
        return self.ref_end_pos - self.ref_start_pos

    def get_aligned_read_length(self):
        return self.read_end_pos - self.read_start_pos

    def get_ref_to_read_ratio(self):
        '''
        Returns the length ratio between the aligned parts of the reference and read.
        '''
        return self.get_aligned_ref_length() / self.get_aligned_read_length()

    def get_read_to_ref_ratio(self):
        '''
        Returns the length ratio between the aligned parts of the read and reference.
        '''
        return 1.0 / self.get_ref_to_read_ratio()

    def read_start_end_positive_strand(self):
        '''
        This function returns the read start/end coordinates for the positive strand of the read.
        For alignments on the positive strand, this is just the normal start/end. But for
        alignments on the negative strand, the coordinates are flipped to the other side.
        '''
        if not self.rev_comp:
            return self.read_start_pos, self.read_end_pos
        else:
            start = self.read.get_length() - self.read_end_pos
            end = self.read.get_length() - self.read_start_pos
            return start, end

    def get_start_soft_clips(self):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if self.cigar_parts[0][-1] == 'S':
            return int(self.cigar_parts[0][:-1])
        else:
            return 0

    def get_end_soft_clips(self):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if self.cigar_parts[-1][-1] == 'S':
            return int(self.cigar_parts[-1][:-1])
        else:
            return 0

    def get_sam_line(self):
        '''
        Returns a SAM alignment line.
        '''
        sam_parts = []
        sam_parts.append(self.read.name) # Query template name
        if self.rev_comp:
            sam_parts.append('16') # Bitwise flag
        else:
            sam_parts.append('0') # Bitwise flag
        sam_parts.append(self.ref.name) # Reference sequence name
        sam_parts.append(str(self.ref_start_pos + 1)) # 1-based leftmost mapping position
        sam_parts.append('255') # Mapping quality (255 means unavailable)
        sam_parts.append(self.cigar) # CIGAR string
        sam_parts.append('*') # Ref. name of the mate/next read (* means unavailable)
        sam_parts.append('0') # Position of the mate/next read (0 means unavailable)
        sam_parts.append('0') # Observed template length (0 means unavailable)

        if self.rev_comp:
            sam_parts.append(reverse_complement(self.read.sequence)) # Segment sequence
            sam_parts.append(self.read.qualities[::-1]) # ASCII of Phred-scaled base quality+33
        else:
            sam_parts.append(self.read.sequence) # Segment sequence
            sam_parts.append(self.read.qualities) # ASCII of Phred-scaled base quality+33

        sam_parts.append('AS:i:' + str(self.raw_score)) # Alignment score generated by aligner

        edit_distance = self.mismatch_count + self.insertion_count + self.deletion_count
        sam_parts.append('NM:i:' + str(edit_distance)) # Edit distance to the reference, including
                                                       # ambiguous bases but excluding clipping
        return '\t'.join(sam_parts) + '\n'

    def is_whole_read(self):
        '''
        Returns True if the alignment covers the entirety of the read.
        '''
        return self.read_start_pos == 0 and self.read_end_gap == 0

    def get_longest_indel_run(self):
        '''
        Returns the longest indel in the alignment.
        '''
        longest_indel_run = 0
        for cigar_part in self.cigar_parts:
            cigar_type = cigar_part[-1]
            if cigar_type == 'I' or cigar_type == 'D':
                longest_indel_run = max(longest_indel_run, int(cigar_part[:-1]))
        return longest_indel_run

    def get_missing_bases_at_start(self):
        '''
        Returns the number of bases at the start of the alignment which are missing in both the
        read and the reference (preventing the alignment from being semi-global).
        '''
        return min(self.read_start_pos, self.ref_start_pos)

    def get_missing_bases_at_end(self):
        '''
        Returns the number of bases at the end of the alignment which are missing in both the read
        and the reference (preventing the alignment from being semi-global).
        '''
        return min(self.read_end_gap, self.ref_end_gap)

    def get_total_missing_bases(self):
        '''
        Returns the number of bases at the start and end of the alignment which are missing in both
        the read and the reference (preventing the alignment from being semi-global).
        '''
        return self.get_missing_bases_at_start() + self.get_missing_bases_at_end()

    def is_very_similar(self, other):
        '''
        Returns true if this alignment and the other alignment seem to be redundant.
        Specifically, the have to be from the same read, the same reference and overlap by 90% or
        more.
        '''
        if self.read.name != other.read.name:
            return False
        if self.ref.name != other.ref.name:
            return False
        if self.rev_comp != other.rev_comp:
            return False

        this_start, this_end = self.read_start_end_positive_strand()
        other_start, other_end = other.read_start_end_positive_strand()
        if other_start > this_end or this_start > other_end:
            return False

        # If the code got here then the alignments are overlapping.
        overlap_size = min(this_end, other_end) - max(this_start, other_start)
        smaller_alignment_length = min(this_end - this_start, other_end - other_start)
        if smaller_alignment_length == 0:
            return False
        return overlap_size / smaller_alignment_length >= 0.9

if __name__ == '__main__':
    main()