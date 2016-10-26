#!/usr/bin/env python3
"""
This script is for polishing a genome that was finished using Unicycler.

Author: Ryan Wick
email: rrwick@gmail.com
"""

import argparse
import os
import shutil
import sys
import subprocess
import collections
import datetime
import statistics
import math
import multiprocessing
from .misc import add_line_breaks_to_sequence, load_fasta, MyHelpFormatter, print_table, \
    get_percentile_sorted, get_pilon_jar_path, colour, bold, bold_green, bold_yellow_underline, \
    dim, get_all_files_in_current_dir, check_file_exists, check_directory_exists


def main():
    args, short, pacbio, nanopore = get_arguments()
    get_tool_paths(args, short, pacbio, nanopore)
    if args.verbosity > 1:
        print()
    clean_up(args)
    current = args.assembly
    round_num = get_starting_round_number()
    copy_file(current, '%03d' % round_num + '_starting_sequence.fasta', args.verbosity)

    if short and (args.min_insert is None or args.max_insert is None):
        get_insert_size_range(args, current)

    if short:
        current, round_num = pilon_polish_small_changes_loop(current, round_num, args)

    if pacbio:
        current, round_num = arrow_polish_small_changes_loop(current, round_num, args, short)

    if nanopore:
        current, round_num = nanopolish_small_changes_loop(current, round_num, args, short)

    current, round_num = polish_large_changes_loop(current, round_num, args,
                                                   short, pacbio, nanopore)

    finish(current, round_num, args)


def get_arguments():
    parser = argparse.ArgumentParser(description='Unicycler polish - hybrid assembly polishing',
                                     formatter_class=MyHelpFormatter)

    assembly_group = parser.add_argument_group('Assembly')
    assembly_group.add_argument('-a', '--assembly', type=str, required=True,
                                help='Input assembly to be polished')

    short_group = parser.add_argument_group('Short reads',
                                            'To polish with short reads (using Pilon), provide '
                                            'two FASTQ files of paired-end reads')
    short_group.add_argument('-1', '--short1', type=str,
                             help='FASTQ file of short reads (first reads in each pair)')
    short_group.add_argument('-2', '--short2', type=str,
                             help='FASTQ file of short reads (second reads in each pair)')

    pacbio_group = parser.add_argument_group('PacBio reads',
                                             'To polish with PacBio reads (using Arrow), provide '
                                             'one of the following')
    pacbio_group.add_argument('--pb_bax', nargs='+', type=str,
                              help='PacBio raw bax.h5 read files')
    pacbio_group.add_argument('--pb_bam', type=str,
                              help='PacBio BAM read file')
    pacbio_group.add_argument('--pb_fasta', type=str,
                              help='FASTA file of PacBio reads')

    nanopore_group = parser.add_argument_group('Nanopore reads',
                                               'To polish with Nanopore reads (using Nanopolish), '
                                               'provide one of the following')
    nanopore_group.add_argument('--on_fast5', type=str,
                                help='Directory containing Oxford Nanopore fast5 files')
    nanopore_group.add_argument('--on_fasta', type=str,
                                help='FASTA file of Oxford Nanopore reads (must have been '
                                     'extracted from fast5 files using nanopolish extract')
    settings_group = parser.add_argument_group('Polishing settings',
                                               'Various settings for polishing behaviour '
                                               '(defaults should work well in most cases)')
    settings_group.add_argument('--min_insert', type=int, default=argparse.SUPPRESS,
                                help='minimum valid short read insert size (default: auto)')
    settings_group.add_argument('--max_insert', type=int, default=argparse.SUPPRESS,
                                help='maximum valid short read insert size (default: auto)')
    settings_group.add_argument('--min_align_length', type=int, default=1000,
                                help='Minimum long read alignment length (default: 1000)')
    settings_group.add_argument('--homopolymer', type=int, default=4,
                                help='Long read polish changes to a homopolymer of this length or '
                                     'greater will be ignored (default: 4)')
    settings_group.add_argument('--large', type=int, default=10,
                                help='Variants of this size or greater will be assess as large '
                                     'variants (default: 10)')
    settings_group.add_argument('--illumina_alt', type=float, default=5.0,
                                help='When assessing long read changes with short read '
                                     'alignments, a variant will only be applied if the '
                                     'alternative occurrences in the short read alignments '
                                     'exceed this percentage (default: 5)')

    other_group = parser.add_argument_group('Other settings')
    other_group.add_argument('--threads', type=int, required=True,
                             help='CPU threads to use in alignment and consensus (default: '
                                  'number of CPUs)')
    other_group.add_argument('--verbosity', type=int, required=False, default=2,
                             help='R|Level of stdout information (0 to 3, default: 2)\n  '
                                  '0 = no stdout, 1 = basic progress indicators, '
                                  '2 = extra info, 3 = debugging info')

    tools_group = parser.add_argument_group('Tool locations',
                                            'If these required tools are not available in your '
                                            'PATH variable, specify their location here '
                                            '(depending on which input reads are used, '
                                            'some of these tools may not be required)')
    tools_group.add_argument('--samtools', type=str, default='samtools',
                             help='path to samtools executable')
    tools_group.add_argument('--bowtie2', type=str, default='bowtie2',
                             help='path to bowtie2 executable')
    tools_group.add_argument('--freebayes', type=str, default='freebayes',
                             help='path to freebayes executable')
    tools_group.add_argument('--pitchfork', type=str, default='',
                             help='Path to Pitchfork installation of PacBio tools (should contain '
                                  'bin and lib directories)')
    tools_group.add_argument('--bax2bam', type=str, default='bax2bam',
                             help='path to bax2bam executable')
    tools_group.add_argument('--pbalign', type=str, default='pbalign',
                             help='path to pbalign executable')
    tools_group.add_argument('--arrow', type=str, default='arrow',
                             help='path to arrow executable')
    tools_group.add_argument('--pilon', type=str, default='pilon*.jar',
                             help='path to pilon jar file')
    tools_group.add_argument('--java', type=str, default='java',
                             help='path to java executable')
    tools_group.add_argument('--ale', type=str, default='ALE',
                             help='path to ALE executable')
    tools_group.add_argument('--bwa', type=str, default='bwa',
                             help='path to bwa executable')
    tools_group.add_argument('--nanopolish', type=str, default='nanopolish',
                             help='path to nanopolish executable')
    tools_group.add_argument('--nanopolish_model_dir', type=str,
                             help='path to nanopolish models directory')

    args = parser.parse_args()

    for f in [args.assembly, args.short1, args.short2, args.pb_bam, args.pb_fasta, args.on_fasta]:
        if f is not None:
            check_file_exists(f)
    if args.pb_bax:
        for f in args.pb_bax:
            check_file_exists(f)

    short_read_input_count = sum(0 if x is None else 1 for x in [args.short1, args.short2])
    if short_read_input_count == 1:
        parser.error('you must provide both short read files (with -1 and -2) or neither of them')

    pacbio_input_count = sum(0 if x is None else 1 for x in [args.pb_bax, args.pb_bam, 
                                                             args.pb_fasta])
    if pacbio_input_count > 1:
        parser.error('only one of the following PacBio inputs can be used: --pb_bax, --pb_bam, '
                     '--pb_fasta')

    nanopore_input_count = sum(0 if x is None else 1 for x in [args.on_fast5, args.on_fasta])
    if nanopore_input_count > 1:
        parser.error('only one of the following Nanopore inputs can be used: --on_fast5, '
                     '--on_fasta')

    if not short_read_input_count and not pacbio_input_count and not nanopore_input_count:
        parser.error('at least one type of input reads is required')

    if args.pb_bam and not os.path.isfile(args.pb_bam + '.pbi'):
        sys.exit('Error: ' + args.pb_bam + '.pbi is missing (PacBio bam read inputs must be '
                                           'indexed with pbindex)')

    short_reads = short_read_input_count == 2
    pacbio_reads = pacbio_input_count == 1
    nanopore_reads = nanopore_input_count == 1

    if nanopore_reads:
        if not args.nanopolish_model_dir:
            parser.error('--nanopolish_model_dir is required when polishing with Nanopore reads')
        if not os.path.isdir(args.nanopolish_model_dir):
            parser.error(args.nanopolish_model_dir + ' is not a directory')
        nanopolish_model_fofn = os.path.join(args.nanopolish_model_dir, 'nanopolish_models.fofn')
        if not os.path.isfile(nanopolish_model_fofn):
            parser.error(nanopolish_model_fofn + ' is missing from ' + args.nanopolish_model_dir)
        args.nanopolish_model_files = [os.path.join(args.nanopolish_model_dir, f)
                                       for f in os.listdir(args.nanopolish_model_dir)
                                       if f.endswith('.model')]
        if not args.nanopolish_model_files:
            parser.error('Nanopolish model files are missing from ' + args.nanopolish_model_dir)
        args.nanopolish_model_files.append(nanopolish_model_fofn)
        if args.on_fast5:
            if not os.path.isdir(args.on_fast5):
                parser.error(args.on_fast5 + ' is not a directory')
            if not any(f.lower().endswith('.fast5') for f in os.listdir(args.on_fast5)):
                parser.error('there are no *.fast5 files in ' + args.on_fast5)

    if args.threads is None:
        args.threads = multiprocessing.cpu_count()
        if args.verbosity > 2:
            print('\nThread count set to', args.threads)

    try:
        args.min_insert
    except AttributeError:
        args.min_insert = None
    try:
        args.max_insert
    except AttributeError:
        args.max_insert = None

    return args, short_reads, pacbio_reads, nanopore_reads


def clean_up(args, pbalign_alignments=True, illumina_alignments=True, nanopore_alignments=True,
             indices=True, large_variants=True, ale_scores=True):
    all_files = get_all_files_in_current_dir()
    files_to_delete = []

    if pbalign_alignments:
        files_to_delete += [f for f in all_files if f.startswith('pbalign_align')]
    if illumina_alignments:
        files_to_delete += [f for f in all_files if f.startswith('illumina_align')]
    if indices:
        files_to_delete += [f for f in all_files if f.endswith('.bt2') or f.endswith('.fai') or
                            f.endswith('.amb') or f.endswith('.ann') or f.endswith('.bwt') or
                            f.endswith('.pac') or f.endswith('.sa') or f.endswith('.bai')]
    if large_variants:
        files_to_delete += [f for f in all_files if f.startswith('large_variant_')]
    if ale_scores:
        files_to_delete += [f for f in all_files if f.startswith('ale.out')]
    if nanopore_alignments:
        files_to_delete += [f for f in all_files if f.startswith('nanopore_align')]
        files_to_delete += [f for f in all_files if f.endswith('_models.fofn')]
        files_to_delete += [f for f in all_files if f.endswith('.model')]

    files_to_delete = sorted(list(set(files_to_delete)))
    if files_to_delete:
        print_command(['rm'] + files_to_delete, args.verbosity)
        for f in files_to_delete:
            os.remove(f)
    if os.path.isdir('temp_pilon'):
        print_command(['rm', '-r', 'temp_pilon'], args.verbosity)
        shutil.rmtree('temp_pilon')


def get_tool_paths(args, short, pacbio, nanopore):
    args.samtools = shutil.which(args.samtools)
    if not args.samtools:
        sys.exit('Error: could not find samtools')

    if short:
        args.bowtie2 = shutil.which(args.bowtie2)
        if not args.bowtie2:
            sys.exit('Error: could not find bowtie2')
        args.bowtie2_build = shutil.which(args.bowtie2 + '-build')
        if not args.bowtie2_build:
            sys.exit('Error: could not find bowtie2-build (it should be in the same place as '
                     'bowtie2)')

        if args.pilon == 'pilon*.jar':
            args.pilon = get_pilon_jar_path(None)
        else:
            args.pilon = get_pilon_jar_path(args.pilon)
        if not args.pilon:
            sys.exit('Error: could not find pilon jar file')

        args.java = shutil.which(args.java)
        if not args.java:
            sys.exit('Error: could not find java')

    if pacbio:
        if args.pitchfork:
            def add_to_env_var(key, new_val):
                if key in os.environ:
                    os.environ[key] = new_val + ':' + os.environ[key]
                else:
                    os.environ[key] = new_val
            add_to_env_var('LD_LIBRARY_PATH', os.path.join(args.pitchfork, 'lib'))
            add_to_env_var('PATH', os.path.join(args.pitchfork, 'bin'))
            add_to_env_var('PYTHONPATH', os.path.join(args.pitchfork, 'lib', 'python2.7',
                                                      'dist-packages'))

        args.pbalign = shutil.which(args.pbalign)
        if not args.pbalign:
            sys.exit('Error: could not find pbalign')

        args.arrow = shutil.which(args.arrow)
        if not args.arrow:
            sys.exit('Error: could not find arrow')

    if nanopore:
        args.bwa = shutil.which(args.bwa)
        if not args.bwa:
            sys.exit('Error: could not find bwa')

        args.nanopolish = shutil.which(args.nanopolish)
        if not args.nanopolish:
            sys.exit('Error: could not find nanopolish')

    args.freebayes = shutil.which(args.freebayes)
    if not args.freebayes:
        sys.exit('Error: could not find freebayes')

    args.ale = shutil.which(args.ale)
    if not args.ale:
        sys.exit('Error: could not find ALE')


def make_reads_bam(args):
    print_round_header('Converting bax.h5 reads to BAM format', args.verbosity)
    args.pb_bam = 'subreads.bam'
    run_command([args.bax2bam] + args.pb_bax, args)
    files_to_delete = []
    for filename in get_all_files_in_current_dir():
        if 'scrap' in filename:
            os.remove(filename)
            files_to_delete.append(filename)
        elif 'subreads' in filename and filename.endswith('.bam'):
            rename_file(filename, args.pb_bam, args.verbosity)
        elif 'subreads' in filename and filename.endswith('.bam.pbi'):
            rename_file(filename, args.pb_bam + '.pbi', args.verbosity)
    print_command(['rm'] + files_to_delete, args.verbosity)
    files = get_all_files_in_current_dir()
    if args.pb_bam not in files:
        sys.exit('Error: bax2bam failed to make ' + args.pb_bam)
    if (args.pb_bam + '.pbi') not in files:
        sys.exit('Error: bax2bam failed to make ' + args.pb_bam + '.pbi')


def make_on_fasta(args):
    print_round_header('Converting FAST5 reads to FASTA format', args.verbosity)
    args.on_fasta = 'nanopore_reads.fasta'
    command = [args.nanopolish, 'extract', args.on_fast5, '-o', args.on_fasta]
    run_command(command, args)
    if args.on_fasta not in get_all_files_in_current_dir():
        sys.exit('Error: nanopolish extract failed to make ' + args.on_fasta)


def pilon_polish_small_changes_loop(current, round_num, args):
    """
    Repeatedly apply small variants using Pilon.
    """
    previously_applied_variants = []
    while True:
        current, round_num, variants = pilon_polish_small_changes(current, round_num, args)

        # If no more changes are suggested, then we're done!
        if not variants:
            break

        # If every one of the suggested changes overlaps with a previous change, then we stop. This
        # is to prevent an infinite loop caused by bases that keep changing back and forth.
        if all_changes_overlap_previous(variants, previously_applied_variants):
            break
        else:
            previously_applied_variants += variants

    return current, round_num


def arrow_polish_small_changes_loop(current, round_num, args, short):
    """
    Repeatedly apply small variants using Arrow.
    """
    # Convert bax.h5 files to a PacBio BAM, if necessary.
    if args.pb_bax and not args.pb_bam:
        make_reads_bam(args)

    previously_applied_variants = []
    while True:
        current, round_num, variants = arrow_polish_small_changes(current, round_num, args, short)

        # If no more changes are suggested, then we're done!
        if not variants:
            break

        # If every one of the suggested changes overlaps with a previous change, then we stop. This
        # is to prevent an infinite loop caused by bases that keep changing back and forth.
        if all_changes_overlap_previous(variants, previously_applied_variants):
            break
        else:
            previously_applied_variants += variants

    return current, round_num


def nanopolish_small_changes_loop(current, round_num, args, short):
    """
    Repeatedly apply small variants using Nanopolish.
    """
    # Convert FAST5 files to a Nanopolish FASTA, if necessary.
    if args.on_fast5 and not args.on_fasta:
        make_on_fasta(args)

    previously_applied_variants = []
    while True:
        current, round_num, variants = nanopolish_small_changes(current, round_num, args, short)

        # If no more changes are suggested, then we're done!
        if not variants:
            break

        # If every one of the suggested changes overlaps with a previous change, then we stop. This
        # is to prevent an infinite loop caused by bases that keep changing back and forth.
        if all_changes_overlap_previous(variants, previously_applied_variants):
            break
        else:
            previously_applied_variants += variants

    return current, round_num


def all_changes_overlap_previous(variants, previous_variants):
    """
    Returns True if all of the variants overlap with a previous variant.
    """
    for variant in variants:
        if not any(variant.overlaps(x) for x in previous_variants):
            return False
    return True


def polish_large_changes_loop(current, round_num, args, short, pacbio, nanopore):
    """
    Large changes are gathered from all available sources (Pilon, Arrow and Nanopolish) and each
    is evaluated using ALE. If the best one beats the ALE score of the input assembly, then another
    round of small variant polishing is done and we repeat!
    """
    while True:
        current, round_num, variants = polish_large_changes(current, round_num, args,
                                                            short, pacbio, nanopore)
        if not variants:
            break

        # If long reads are available, we use them for the follow-up small variant polishing.
        if pacbio:
            current, round_num = arrow_polish_small_changes_loop(current, round_num, args, short)
        if nanopore:
            current, round_num = nanopolish_small_changes_loop(current, round_num, args, short)

        # We only use short reads for follow-up small variant polishing if there are no long reads.
        if not (pacbio or nanopore):
            current, round_num = pilon_polish_small_changes_loop(current, round_num, args)

    return current, round_num


def pilon_polish_small_changes(fasta, round_num, args):
    round_num += 1
    print_round_header('Round ' + str(round_num) + ': Pilon polish, small variants', args.verbosity)

    variants_file = '%03d' % round_num + '_1_pilon.changes'
    polished_fasta = '%03d' % round_num + '_2_polish.fasta'

    variants = get_pilon_variants(fasta, args, 'bases', variants_file)

    if not variants:
        print_empty_result(args.verbosity)
        return fasta, round_num, 0
    else:
        apply_variants(fasta, variants, polished_fasta)
        variant_rows = [x.get_output_row(False) for x in variants]
        print_small_variant_table(variant_rows, False, args.verbosity)
        print_result(variants, polished_fasta, args.verbosity)
        return polished_fasta, round_num, variants


def nanopolish_small_changes(fasta, round_num, args, short):
    round_num += 1
    print_round_header('Round ' + str(round_num) + ': Nanopore polish, small variants',
                       args.verbosity)

    raw_variants_file = '%03d' % round_num + '_1_raw_variants'
    filtered_variants_file = '%03d' % round_num + '_2_filtered_variants'
    polished_fasta = '%03d' % round_num + '_3_polish.fasta'

    align_nanopore_reads(fasta, args)
    run_nanopolish(fasta, args, raw_variants_file)
    raw_variants = load_variants_from_nanopolish(raw_variants_file, fasta, args)
    small_variants = [x for x in raw_variants if not x.large]

    if short:
        align_illumina_reads(fasta, args, local=False)
        for variant in small_variants:
            variant.assess_against_illumina_alignments(fasta, args)
    clean_up(args)

    filtered_variants = filter_small_variants(small_variants, raw_variants_file,
                                              filtered_variants_file, args, short)
    if filtered_variants:
        apply_variants(fasta, filtered_variants, polished_fasta)
        current = polished_fasta
    else:
        current = fasta
    print_result(filtered_variants, polished_fasta, args.verbosity)
    return current, round_num, filtered_variants


def arrow_polish_small_changes(fasta, round_num, args, short):
    round_num += 1
    print_round_header('Round ' + str(round_num) + ': PacBio polish, small variants',
                       args.verbosity)

    raw_variants_file = '%03d' % round_num + '_1_raw_variants.gff'
    filtered_variants_file = '%03d' % round_num + '_2_filtered_variants.gff'
    polished_fasta = '%03d' % round_num + '_3_polish.fasta'

    align_pacbio_reads(fasta, args)
    run_arrow(fasta, args, raw_variants_file)
    raw_variants = load_variants_from_arrow(raw_variants_file, fasta, args)
    small_variants = [x for x in raw_variants if not x.large]

    if short:
        align_illumina_reads(fasta, args, local=False)
        for variant in small_variants:
            variant.assess_against_illumina_alignments(fasta, args)
    clean_up(args)

    filtered_variants = filter_small_variants(small_variants, raw_variants_file,
                                              filtered_variants_file, args, short)
    if filtered_variants:
        apply_variants(fasta, filtered_variants, polished_fasta)
        current = polished_fasta
    else:
        current = fasta
    print_result(filtered_variants, polished_fasta, args.verbosity)
    return current, round_num, filtered_variants


def polish_large_changes(fasta, round_num, args, short, pacbio, nanopore):
    round_num += 1
    print_round_header('Round ' + str(round_num) + ': large variants', args.verbosity)

    variants = []
    file_num = 0
    if short:
        file_num += 1
        pilon_variants_file = '%03d' % round_num + '_' + str(file_num) + '_pilon.changes'
        variants += get_pilon_variants(fasta, args, 'local', pilon_variants_file)
    if nanopore:
        file_num += 1
        nano_variants_file = '%03d' % round_num + '_' + str(file_num) + '_nanopolish_variants'
        variants += get_nanopolish_large_variants(fasta, args, nano_variants_file)
    if pacbio:
        file_num += 1
        arrow_variants_file = '%03d' % round_num + '_' + str(file_num) + '_arrow.gff'
        variants += get_arrow_large_variants(fasta, args, arrow_variants_file)

    if not variants:
        clean_up(args)
        print_empty_result(args.verbosity)
        return fasta, round_num, []

    ale_outputs = '%03d' % round_num + '_' + str(file_num+1) + '_ALE_output'
    filtered_variants_file = '%03d' % round_num + '_' + str(file_num+2) + '_filtered_variants'
    polished_fasta = '%03d' % round_num + '_' + str(file_num+3) + '_polish.fasta'

    open(filtered_variants_file, 'a').close()
    open(ale_outputs, 'a').close()

    initial_ale_score = run_ale(fasta, args, ale_outputs)
    best_ale_score = initial_ale_score

    best_modification = None
    applied_variant = []
    for i, variant in enumerate(variants):
        modified_assembly = 'large_variant_' + str(i+1) + '.fasta'
        apply_variants(fasta, [variant], modified_assembly)
        variant.ale_score = run_ale(modified_assembly, args, ale_outputs)
        if variant.ale_score > best_ale_score:
            best_ale_score = variant.ale_score
            best_modification = modified_assembly
            applied_variant = [variant]
            save_large_variants(applied_variant, filtered_variants_file)

    if best_modification:
        rename_file(best_modification, polished_fasta, args.verbosity)
    else:
        copy_file(fasta, polished_fasta, args.verbosity)
    clean_up(args)

    print_large_variant_table(variants, best_ale_score, initial_ale_score)
    print_result(applied_variant, polished_fasta, args.verbosity)

    current = polished_fasta if applied_variant else fasta
    return current, round_num, applied_variant


def run_ale(fasta, args, all_ale_outputs):
    """
    ALE is run in --metagenome mode because this polishing script is presumed to be used on
    completed bacterial genomes, where each contig is different replicon (chromosome or plasmid)
    with potentially different depth.
    """
    ale_output = 'ale.out'
    ale_score = float('-inf')
    previous_output_exists = os.path.getsize(all_ale_outputs) > 0

    align_illumina_reads(fasta, args, local=False, keep_unaligned=True)

    run_command([args.ale,
                 '--nout',
                 '--metagenome',
                 'illumina_alignments.bam', fasta, ale_output], args)
    if not os.path.isfile(ale_output):
        sys.exit('Error: ALE did not generate ' + ale_output)

    with open(ale_output, 'rt') as ale_output_file:
        with open(all_ale_outputs, 'at') as all_ale_outputs_file:
            if previous_output_exists:
                all_ale_outputs_file.write('\n\n')
            all_ale_outputs_file.write(fasta + ':\n')
            for line in ale_output_file:
                all_ale_outputs_file.write(line)
                if 'ALE_score:' in line and ale_score == float('-inf'):
                    ale_score = float(line.split('ALE_score:')[1].strip().split()[0])

    clean_up(args, large_variants=False)
    return ale_score


def align_pacbio_reads(fasta, args):
    command = [args.pbalign, '--nproc', str(args.threads),
               '--minLength', str(args.min_align_length),
               '--algorithmOptions="--minRawSubreadScore 800 --bestn 1"',
               args.pb_bam, fasta, 'pbalign_alignments.bam']
    run_command(command, args)
    files = get_all_files_in_current_dir()
    if 'pbalign_alignments.bam' not in files:
        sys.exit('Error: pbalign failed to make pbalign_alignments.bam')
    if 'pbalign_alignments.bam.pbi' not in files:
        sys.exit('Error: pbalign failed to make pbalign_alignments.bam.pbi')
    if 'pbalign_alignments.bam.bai' not in files:
        sys.exit('Error: pbalign failed to make pbalign_alignments.bam.bai')


def align_nanopore_reads(fasta, args):
    bam = 'nanopore_alignments.bam'

    run_command([args.bwa, 'index', fasta], args)

    bwa_command = [args.bwa, 'mem', '-x', 'ont2d', '-t', str(args.threads), fasta, args.on_fasta]
    samtools_view_command = [args.samtools, 'view', '-hu', '-']
    samtools_sort_command = [args.samtools, 'sort', '-@', str(args.threads), '-o', bam, '-']
    print_command(bwa_command + ['|'] + samtools_view_command + ['|'] + samtools_sort_command,
                  args.verbosity)

    bwa = subprocess.Popen(bwa_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    samtools_view = subprocess.Popen(samtools_view_command, stdin=bwa.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    bwa.stdout.close()
    samtools_sort = subprocess.Popen(samtools_sort_command, stdin=samtools_view.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    samtools_view.stdout.close()
    out, err = samtools_sort.communicate()
    if args.verbosity > 2:
        out = bwa.stderr.read() + samtools_view.stderr.read() + out + err
        print(dim(out.decode()))

    run_command([args.samtools, 'index', bam], args)


def align_illumina_reads(fasta, args, make_bam_index=True, local=False, keep_unaligned=False,
                         large_insert_range=False):
    index = 'bowtie_index'
    bam = 'illumina_alignments.bam'

    run_command([args.bowtie2_build, fasta, index], args)

    if large_insert_range:
        min_insert, max_insert = 0, 2000
    else:
        min_insert, max_insert = args.min_insert, args.max_insert

    if local:
        bowtie2_command = [args.bowtie2, '--local', '--very-sensitive-local']
    else:
        bowtie2_command = [args.bowtie2, '--end-to-end', '--very-sensitive']
    if not keep_unaligned:
        bowtie2_command += ['--no-unal']
    bowtie2_command += ['--threads', str(args.threads),
                        '-I', str(min_insert), '-X', str(max_insert),
                        '-x', index, '-1', args.short1, '-2', args.short2]
    samtools_view_command = [args.samtools, 'view', '-hu', '-']
    samtools_sort_command = [args.samtools, 'sort', '-@', str(args.threads), '-o', bam, '-']
    print_command(bowtie2_command + ['|'] + samtools_view_command + ['|'] + samtools_sort_command,
                  args.verbosity)

    bowtie2 = subprocess.Popen(bowtie2_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    samtools_view = subprocess.Popen(samtools_view_command, stdin=bowtie2.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    bowtie2.stdout.close()
    samtools_sort = subprocess.Popen(samtools_sort_command, stdin=samtools_view.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    samtools_view.stdout.close()
    out, err = samtools_sort.communicate()
    if args.verbosity > 2:
        out = bowtie2.stderr.read() + samtools_view.stderr.read() + out + err
        print(dim(out.decode()))

    if make_bam_index:
        run_command([args.samtools, 'index', bam], args)


def run_pilon(fasta, args, raw_pilon_changes_filename, fix_type):
    pilon_command = [args.java, '-jar', args.pilon, '--genome', fasta,
                     '--frags', 'illumina_alignments.bam', '--fix', fix_type, '--changes',
                     '--outdir', 'temp_pilon']
    run_command(pilon_command, args)

    pilon_changes = os.path.join('temp_pilon', 'pilon.changes')
    if not os.path.isfile(pilon_changes):
        sys.exit('Pilon did not produce pilon.changes')
    copy_file(pilon_changes, raw_pilon_changes_filename, args.verbosity)


def get_pilon_variants(fasta, args, fix_type, raw_pilon_changes):
    # Pilon needs local alignment to help spot misassembly regions and unaligned reads to use
    # when reassembling.
    align_illumina_reads(fasta, args, local=True, keep_unaligned=True)
    run_pilon(fasta, args, raw_pilon_changes, fix_type)
    clean_up(args)
    return load_variants_from_pilon_changes(raw_pilon_changes, fasta, args.large)


def get_nanopolish_large_variants(fasta, args, raw_nanopolish_variants):
    align_nanopore_reads(fasta, args)
    run_nanopolish(fasta, args, raw_nanopolish_variants)
    raw_variants = load_variants_from_nanopolish(raw_nanopolish_variants, fasta, args)
    return [x for x in raw_variants if x.large]


def get_arrow_large_variants(fasta, args, raw_arrow_variants):
    align_pacbio_reads(fasta, args)
    run_arrow(fasta, args, raw_arrow_variants)
    raw_variants = load_variants_from_arrow(raw_arrow_variants, fasta, args)
    return [x for x in raw_variants if x.large]


def run_arrow(fasta, args, raw_variants_filename):
    subprocess.call([args.samtools, 'faidx', fasta])
    command = [args.arrow, 'pbalign_alignments.bam', '-j', str(args.threads),
               '--noEvidenceConsensusCall', 'reference', '-r', fasta, '-o', raw_variants_filename]
    run_command(command, args)
    if raw_variants_filename not in get_all_files_in_current_dir():
        sys.exit('Error: Arrow failed to make ' + raw_variants_filename)


def run_nanopolish(fasta, args, raw_variants_filename):
    copy_files(args.nanopolish_model_files, '.', args.verbosity)
    bam_1 = 'nanopore_alignments.bam'
    bam_2 = 'nanopolish_eventalign.bam'

    nanopolish_command = [args.nanopolish, 'eventalign', '-t', str(args.threads), '--sam',
                          '-r', args.on_fasta, '-b', bam_1, '-g', fasta,
                          '--models', args.nanopolish_model_files[-1]]
    samtools_view_command = [args.samtools, 'view', '-hu', '-']
    samtools_sort_command = [args.samtools, 'sort', '-@', str(args.threads), '-o', bam_2, '-']

    print_command(nanopolish_command + ['|'] + samtools_view_command + ['|'] +
                  samtools_sort_command, args.verbosity)

    nanopolish = subprocess.Popen(nanopolish_command, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
    samtools_view = subprocess.Popen(samtools_view_command, stdin=nanopolish.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    nanopolish.stdout.close()
    samtools_sort = subprocess.Popen(samtools_sort_command, stdin=samtools_view.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    samtools_view.stdout.close()
    out, err = samtools_sort.communicate()
    if args.verbosity > 2:
        out = nanopolish.stderr.read() + samtools_view.stderr.read() + out + err
        print(dim(out.decode()))

    run_command([args.samtools, 'index', bam_2], args)

    nanopolish_command = [args.nanopolish, 'variants', '-r', args.on_fasta, '-b', bam_1,
                          '-g', fasta, '-e', bam_2, '-t', str(args.threads),
                          '--min-candidate-frequency', '0.1', '-o', raw_variants_filename,
                          '--models', args.nanopolish_model_files[-1]]

    run_command(nanopolish_command, args)
    if raw_variants_filename not in get_all_files_in_current_dir():
        sys.exit('Error: Nanopolish failed to make ' + raw_variants_filename)

    clean_up(args)


def filter_small_variants(raw_variants, raw_variants_gff, filtered_variants_gff, args,
                          short_read_assessed):
    filtered_variants = []
    variant_rows = []
    for variant in raw_variants:
        variant_row = variant.get_output_row(short_read_assessed)

        # If we are assessing small variants with short reads, then both the AO percentage and
        # homopolymer length are used to filter variants.
        if short_read_assessed:
            passed = (variant.illumina_alt_percent and                       # must have an alt%
                      variant.illumina_alt_percent >= args.illumina_alt and  # must have a big alt%
                      variant.homo_size_before < args.homopolymer)           # can't be homopolymer

        # If we are not assessing small variants with short reads, then we only filter based on
        # homopolymer length.
        else:
            passed = variant.homo_size_before < args.homopolymer

        if passed:
            filtered_variants.append(variant)
            variant_row.append('PASS')
        else:
            variant_row.append('FAIL')
        variant_rows.append(variant_row)

    print_small_variant_table(variant_rows, short_read_assessed, args.verbosity)

    with open(filtered_variants_gff, 'wt') as new_gff:
        with open(raw_variants_gff, 'rt') as old_gff:
            for line in old_gff:
                if line.startswith('##'):
                    new_gff.write(line)
        for variant in filtered_variants:
            new_gff.write(variant.original_gff_line + '\n')
    return filtered_variants


def save_large_variants(variants, filename):
    with open(filename, 'wt') as output_file:
        for variant in variants:
            output_file.write(variant.get_original_line() + '\n')


def apply_variants(in_fasta, variants, out_fasta):
    """
    This function creates a new FASTA file by applying the variants to an existing FASTA file.
    """
    in_seqs = collections.OrderedDict(load_fasta(in_fasta))
    out_seqs = collections.OrderedDict()
    for name, seq in in_seqs.items():
        seq_variants = sorted([x for x in variants if x.ref_name == name],
                              key=lambda x: x.start_pos)
        new_seq = ''
        pos = 0
        for variant in seq_variants:
            new_seq += seq[pos:variant.start_pos]
            new_seq += variant.variant_seq
            pos = variant.end_pos
        new_seq += seq[pos:]
        out_seqs[name] = new_seq
    with open(out_fasta, 'wt') as fasta:
        for name, seq in out_seqs.items():
            fasta.write('>' + name + '\n')
            fasta.write(add_line_breaks_to_sequence(seq, 60))


def print_command(command, verbosity):
    if verbosity > 1:
        timestamp = '{:%Y-%m-%d %H:%M:%S}'.format(datetime.datetime.now())
        command = [os.path.basename(command[0])] + command[1:]  # Remove path from program name
        print(bold(timestamp) + '   ' + ' '.join(command), flush=True)


def print_round_header(text, verbosity):
    if verbosity > 0:
        print('\n')
        print(bold_yellow_underline(text), flush=True)


def print_empty_result(verbosity):
    if verbosity > 1:
        print()
    if verbosity > 0:
        print('No variants found', flush=True)


def print_result(variants, fasta, verbosity):
    if verbosity > 1:
        print()
    if verbosity > 0:
        if variants:
            var = ' variant' if len(variants) == 1 else ' variants'
            print(str(len(variants)) + var + ' applied, saved to ' + fasta, flush=True)
        else:
            print('No variants applied', flush=True)


def finish(current, round_num, args):
    round_num += 1
    final_fasta = '%03d' % round_num + '_final_polish.fasta'
    if args.verbosity > 1:
        print()
    copy_file(current, final_fasta, args.verbosity)
    if args.verbosity > 0:
        print()
        print('All done! Final assembly: ' + bold_green(final_fasta), flush=True)
        print()


def run_command(command, args):
    print_command(command, args.verbosity)
    try:
        out = subprocess.check_output(command, stderr=subprocess.STDOUT, shell=False)

        # bowtie2-build outputs too much, even for verbose mode.
        if args.verbosity > 2 and 'bowtie2-build' not in command[0]:
            print(dim(out.decode()))
    except subprocess.CalledProcessError as e:
        sys.exit(e.output.decode())


def load_variants_from_arrow(gff_file, fasta, args):
    reference = dict(load_fasta(fasta))
    variants = []
    with open(gff_file, 'rt') as gff:
        for line in gff:
            line = line.strip()
            if line and not line.startswith('##'):
                variants.append(Variant(reference, args.large, gff_line=line))
    return variants


def load_variants_from_nanopolish(nanopolish_file, fasta, args):
    reference = dict(load_fasta(fasta))
    variants = []
    with open(nanopolish_file, 'rt') as nanopolish:
        for line in nanopolish:
            line = line.strip()
            # TO DO
            # TO DO
            # TO DO
            # TO DO
            # TO DO
    return variants


def load_variants_from_pilon_changes(pilon_changes_file, fasta, large_var_size):
    reference = dict(load_fasta(fasta))
    variants = []
    with open(pilon_changes_file, 'rt') as changes:
        for line in changes:
            line = line.strip()
            if line:
                variants.append(Variant(reference, large_var_size, changes_line=line))
    return variants


def has_multiple_bases(seq):
    seq = seq.upper()
    base_counts = [seq.count('A'), seq.count('C'), seq.count('G'), seq.count('T')]
    base_counts = [x for x in base_counts if x > 0]
    return len(base_counts) > 1


def homopolymer_size(seq, pos):
    size = 1
    starting_base = seq[pos]
    forward_pos = pos + 1
    while True:
        if forward_pos >= len(seq) or seq[forward_pos] != starting_base:
            break
        size += 1
        forward_pos += 1
    reverse_pos = pos - 1
    while True:
        if reverse_pos < 0 or seq[reverse_pos] != starting_base:
            break
        size += 1
        reverse_pos -= 1
    return size


class Variant(object):
    def __init__(self, reference, large_var_size, gff_line=None, changes_line=None):
        self.original_gff_line = gff_line
        self.original_changes_line = changes_line

        if gff_line:
            # https://github.com/PacificBiosciences/GenomicConsensus/blob/master/doc/VariantsGffSpecification.rst
            line_parts = gff_line.split('\t')
            attributes = {x.split('=')[0]: x.split('=')[1] for x in line_parts[8].split(';')}
            self.source = 'Arrow'
            self.ref_name = line_parts[0]
            self.type = line_parts[2]
            self.ref_seq = attributes['reference'].replace('.', '')
            self.variant_seq = attributes['variantSeq'].replace('.', '')
            self.start_pos = int(line_parts[3]) - 1
            self.end_pos = self.start_pos + len(self.ref_seq)

        elif changes_line:
            # https://github.com/broadinstitute/pilon/wiki/Output-File-Descriptions
            line_parts = changes_line.split(' ')
            self.source = 'Pilon'
            self.ref_name = line_parts[0].split(':')[0]
            self.ref_seq = line_parts[2].replace('.', '')
            self.variant_seq = line_parts[3].replace('.', '')
            self.start_pos = int(line_parts[0].split(':')[1].split('-')[0]) - 1
            self.end_pos = self.start_pos + len(self.ref_seq)
            if len(self.ref_seq) > len(self.variant_seq):
                self.type = 'deletion'
            elif len(self.ref_seq) < len(self.variant_seq):
                self.type = 'insertion'
            else:
                self.type = 'substitution'

        full_ref_sequence = reference[self.ref_name]

        # Figure out the change if homopolymer length (if applicable) using these rules:
        # Only indels change homopolymer length
        if self.type != 'insertion' and self.type != 'deletion':
            self.homo_size_before = 0
            self.homo_size_after = 0
        # Any indel which contains multiple different bases doesn't change the homopolymer length
        elif has_multiple_bases(self.ref_seq) or has_multiple_bases(self.variant_seq):
            self.homo_size_before = 0
            self.homo_size_after = 0
        # Insertions only change the homopolymer length if they are inserting the same base
        elif self.type == 'insertion' and self.variant_seq[0] != full_ref_sequence[self.start_pos]:
            self.homo_size_before = 0
            self.homo_size_after = 0
        else:
            self.homo_size_before = homopolymer_size(full_ref_sequence, self.start_pos)
            if self.type == 'insertion':
                self.homo_size_after = self.homo_size_before + len(self.variant_seq)
            else:  # deletion
                self.homo_size_after = max(self.homo_size_before - len(self.ref_seq), 0)

        # Categorise indel variants as small or large. These two categories are assessed
        # differently regarding whether or not to apply them. All Pilon changes are 'large',
        # because it is run only asking for 'local' fixes, not 'bases' fixes.
        if self.source == 'Pilon':
            self.large = True
        if self.type == 'insertion':
            self.large = len(self.variant_seq) >= large_var_size
        elif self.type == 'deletion':
            self.large = len(self.ref_seq) >= large_var_size
        else:
            self.large = False

        self.ro = 0
        self.ao = 0
        self.illumina_alt_percent = None
        self.ale_score = float('-inf')

    def assess_against_illumina_alignments(self, reference_fasta, args):
        """
        To assess a variant against the illumina alignments, we use freebayes to see how many
        alternate bases are present at the variant location and what fraction of the bases are
        alternates. A high alternate fraction indicates that something's a bit screwy and we
        should probably apply the PacBio polishing suggestion.
        """
        ref_location = self.ref_name + ':' + str(self.start_pos - 5) + '-' + str(self.end_pos + 5)
        freebayes_command = [args.freebayes,
                             '-f', reference_fasta,
                             '-p', '1',
                             '-r', ref_location,
                             '--report-monomorphic',
                             '--min-alternate-fraction', '0',
                             '--pooled-continuous',
                             '--min-alternate-count', '1',
                             '--haplotype-length', '0',
                             'illumina_alignments.bam']
        freebayes_out = subprocess.check_output(freebayes_command, stderr=subprocess.STDOUT)
        freebayes_lines = [x for x in freebayes_out.decode().split('\n')
                           if x and not x.startswith('#')]
        for line in freebayes_lines:
            line_parts = line.split('\t')

            # We will take the max alt percent for all the freebayes variants which overlap with
            # this variant (with an extra margin of 1 base on either side of this variant).
            freebayes_start_pos = int(line_parts[1]) - 1
            freebayes_end_pos = freebayes_start_pos + len(line_parts[3]) - 1  # inclusive end
            overlap = any(freebayes_start_pos <= x <= freebayes_end_pos
                          for x in range(self.start_pos - 1, self.end_pos + 1))
            if overlap:
                if args.verbosity > 2:
                    print(line)
                ref_occurrences = int(line.split(';RO=')[1].split(';')[0])
                if ';AO=' in line:
                    alt_occurrences = sum(int(x) for x in
                                          line.split(';AO=')[1].split(';')[0].split(','))
                else:
                    alt_occurrences = 0
                total_occurrences = ref_occurrences + alt_occurrences
                if total_occurrences:
                    alt_percent = 100.0 * alt_occurrences / total_occurrences
                else:
                    alt_percent = 0.0
                if self.illumina_alt_percent is None or alt_percent >= self.illumina_alt_percent:
                    self.ao = alt_occurrences
                    self.ro = ref_occurrences
                    self.illumina_alt_percent = alt_percent
            elif args.verbosity > 2:
                print(dim(line))

    def get_output_row(self, short_read_assessed):
        if self.homo_size_before > 1:
            variant_type = 'homo ' + str(self.homo_size_before) + ' -> ' + str(
                self.homo_size_after)
        else:
            variant_type = self.type
            if self.large:
                variant_type = 'large ' + variant_type

        ref_seq = self.ref_seq if self.ref_seq else '.'
        variant_seq = self.variant_seq if self.variant_seq else '.'

        if not short_read_assessed:
            return [self.ref_name, str(self.start_pos + 1), ref_seq, variant_seq, variant_type]
        else:
            # noinspection PyStringFormat
            alt_percent = 'n/a' if self.illumina_alt_percent is None \
                else '%.1f' % self.illumina_alt_percent

            return [self.ref_name, str(self.start_pos + 1), ref_seq, variant_seq, variant_type,
                    str(self.ao), str(self.ro), alt_percent]

    def get_original_line(self):
        if self.original_gff_line:
            return self.original_gff_line
        else:
            return self.original_changes_line

    def overlaps(self, other):
        """
        Returns True if this variant and the other overlap in terms of reference position.
        """
        range_1 = range(self.start_pos, self.end_pos + 1)
        range_2 = range(other.start_pos, other.end_pos + 1)
        return bool(set(range_1) & set(range_2))


def print_small_variant_table(rows, short_read_assessed, verbosity):
    if verbosity < 2:
        return
    print()
    if short_read_assessed:
        header = ['Contig', 'Position', 'Ref', 'Alt', 'Type', 'AO', 'RO', 'AO%', 'Result']
        print_table([header] + rows, alignments='LRLLLRRRR',
                    sub_colour={'PASS': 'green', 'FAIL': 'red'})
    else:
        header = ['Contig', 'Position', 'Ref', 'Alt', 'Type']
        print_table([header] + rows, alignments='LRLLL')


def print_simple_large_variant_table(variants):
    table = [['Contig', 'Position', 'Ref', 'Alt']]
    for v in variants:
        ref_seq = v.ref_seq if v.ref_seq else '.'
        variant_seq = v.variant_seq if v.variant_seq else '.'
        table.append([v.ref_name, str(v.start_pos), ref_seq, variant_seq])
    print_table(table, alignments='LRLL')


def print_large_variant_table(variants, best_ale_score, initial_ale_score):
    print()
    table = [['Source', 'Contig', 'Position', 'Ref', 'Alt', 'ALE score']]
    text_colour = 'green' if initial_ale_score == best_ale_score else 'red'
    table.append(['No variant', '', '', '', '', colour('%.6f' % initial_ale_score, text_colour)])
    for v in variants:
        ref_seq = v.ref_seq if v.ref_seq else '.'
        variant_seq = v.variant_seq if v.variant_seq else '.'
        text_colour = 'green' if v.ale_score == best_ale_score else 'red'
        ale_score_str = colour('%.6f' % v.ale_score, text_colour)
        table.append([v.source, v.ref_name, str(v.start_pos), ref_seq, variant_seq, ale_score_str])
    print_table(table, alignments='LLRLLR')


def analyse_insert_sizes(args):
    insert_sizes = []
    command = [args.samtools, 'view', 'illumina_alignments.bam']
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for sam_line in iter(process.stdout.readline, b''):
            sam_line = sam_line.decode()
            try:
                sam_parts = sam_line.split('\t')
                sam_flags = int(sam_parts[1])
                if sam_flags & 2:
                    insert_size = float(sam_parts[8])
                    if 0.0 < insert_size < 10000.0:  # Just a sanity check...
                        insert_sizes.append(insert_size)
            except (ValueError, IndexError):
                pass
        process.wait()
    except subprocess.CalledProcessError as e:
        sys.exit(e.output.decode())
    insert_sizes = sorted(insert_sizes)
    min_insert = math.floor(get_percentile_sorted(insert_sizes, 2.5))
    mean_insert = statistics.mean(insert_sizes)
    max_insert = math.ceil(get_percentile_sorted(insert_sizes, 97.5))
    return min_insert, mean_insert, max_insert


def print_insert_sizes(min_insert, mean_insert, max_insert):
    print()
    print(' 2.5th percentile:', min_insert)
    print(' mean insert size:', '\033[1m' + '%.1f' % mean_insert + '\033[0m')
    print('97.5th percentile:', max_insert, flush=True)


def get_starting_round_number():
    round_num = 0
    while any(f.startswith('%03d' % round_num) for f in get_all_files_in_current_dir()):
        round_num += 1
    return round_num


def get_insert_size_range(args, fasta):
    print_round_header('Determining insert size', args.verbosity)
    align_illumina_reads(fasta, args, make_bam_index=False, large_insert_range=True)
    min_insert, mean_insert, max_insert = analyse_insert_sizes(args)
    if min_insert == 0 or max_insert == 0:
        sys.exit('Error: could not determine Illumina reads insert size')
    clean_up(args)
    if args.min_insert is None:
        args.min_insert = min_insert
    if args.max_insert is None:
        args.max_insert = max_insert
    if args.verbosity > 0:
        print_insert_sizes(min_insert, mean_insert, max_insert)
        print('\nValid insert size range:', args.min_insert, 'to', args.max_insert)


def copy_file(source, destination, verbosity):
    print_command(['cp', source, destination], verbosity)
    shutil.copy(source, destination)


def copy_files(source_files, destination_dir, verbosity):
    print_command(['cp'] + source_files + [destination_dir], verbosity)
    for source in source_files:
        shutil.copy(source, destination_dir)


def rename_file(old_name, new_name, verbosity):
    print_command(['mv', old_name, new_name], verbosity)
    os.rename(old_name, new_name)