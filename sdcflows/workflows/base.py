# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""SDC workflows coordination."""
from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu
from nipype import logging

from niworkflows.engine.workflows import LiterateWorkflow as Workflow


LOGGER = logging.getLogger('nipype.workflow')
FMAP_PRIORITY = {
    'epi': 0,
    'fieldmap': 1,
    'phasediff': 2,
    'syn': 3,
}

DEFAULT_MEMORY_MIN_GB = 0.01


def init_sdc_estimate_wf(fmaps, epi_meta, omp_nthreads=1, debug=False, ignore=None):
    """
    Build a :abbr:`SDC (susceptibility distortion correction)` workflow.

    This workflow implements the heuristics to choose an estimation
    methodology for :abbr:`SDC (susceptibility distortion correction)`.
    When no field map information is present within the BIDS inputs,
    the EXPERIMENTAL "fieldmap-less SyN" can be performed, using
    the ``--use-syn`` argument. When ``--force-syn`` is specified,
    then the "fieldmap-less SyN" is always executed and reported
    despite of other fieldmaps available with higher priority.
    In the latter case (some sort of fieldmap(s) is available and
    ``--force-syn`` is requested), then the :abbr:`SDC (susceptibility
    distortion correction)` method applied is that with the
    highest priority.

    Parameters
    ----------
    fmaps : list of pybids dicts
        A list of dictionaries with the available fieldmaps
        (and their metadata using the key ``'metadata'`` for the
        case of :abbr:`PEPOLAR (Phase-Encoding POLARity)` fieldmaps).
    epi_meta : dict
        BIDS metadata dictionary corresponding to the
        :abbr:`EPI (echo-planar imaging)` run (i.e., suffix ``bold``,
        ``sbref``, or ``dwi``) for which the fieldmap is being estimated.
    omp_nthreads : int
        Maximum number of threads an individual process may use
    debug : bool
        Enable debugging outputs

    Inputs
    ------
    epi_file
        A reference image calculated at a previous stage
    epi_brain
        Same as above, but brain-masked
    epi_mask
        Brain mask for the run
    t1w_brain
        T1w image, brain-masked, for the fieldmap-less SyN method
    std2anat_xfm
        Standard-to-T1w transform generated during spatial
        normalization (only for the fieldmap-less SyN method).

    Outputs
    -------
    epi_file
        An unwarped EPI scan reference
    epi_mask
        The corresponding new mask after unwarping
    epi_brain
        Brain-extracted, unwarped EPI scan reference
    out_warp
        The deformation field to unwarp the susceptibility distortions
    syn_ref
        If ``--force-syn``, an unwarped EPI scan reference with this
        method (for reporting purposes)

    """
    workflow = Workflow(name='sdc_estimate_wf' if fmaps else 'sdc_bypass_wf')
    inputnode = pe.Node(niu.IdentityInterface(
        fields=['epi_file', 'epi_brain', 'epi_mask', 't1w_brain', 'std2anat_xfm']),
        name='inputnode')

    outputnode = pe.Node(niu.IdentityInterface(
        fields=['output_ref', 'epi_mask', 'epi_brain',
                'out_warp', 'syn_ref', 'method']),
        name='outputnode')

    # No fieldmaps - forward inputs to outputs
    ignored = False if ignore is None else 'fieldmaps' in ignore
    if not fmaps or ignored:
        workflow.__postdesc__ = """\
Susceptibility distortion correction (SDC) has been skipped because the
dataset does not contain extra field map acquisitions correctly described
with metadata, and the experimental SDC-SyN method was not explicitly selected.
"""
        outputnode.inputs.method = 'None'
        workflow.connect([
            (inputnode, outputnode, [('epi_file', 'output_ref'),
                                     ('epi_mask', 'epi_mask'),
                                     ('epi_brain', 'epi_brain')]),
        ])
        return workflow

    workflow.__postdesc__ = """\
Based on the estimated susceptibility distortion, an unwarped
EPI (echo-planar imaging) reference was calculated for a more
accurate co-registration with the anatomical reference.
"""

    only_syn = 'syn' in fmaps and len(fmaps) == 1

    # PEPOLAR path
    if 'epi' in fmaps:
        from .pepolar import init_pepolar_unwarp_wf, check_pes

        # SyN works without this metadata
        if epi_meta.get('PhaseEncodingDirection') is None:
            raise ValueError(
                'PhaseEncodingDirection is not defined within the metadata retrieved '
                'for the intended EPI (DWI, BOLD, or SBRef) run.')
        outputnode.inputs.method = 'PEB/PEPOLAR (phase-encoding based / PE-POLARity)'

        fmaps_epi = [(v[0], v[1].get('PhaseEncodingDirection'))
                     for v in fmaps['epi']]

        if not all(list(zip(*fmaps_epi))[1]):
            raise ValueError(
                'At least one of the EPI runs with alternative phase-encoding '
                'blips is missing the required "PhaseEncodingDirection" metadata entry.')

        # Find matched PE directions
        matched_pe = check_pes(fmaps_epi, epi_meta['PhaseEncodingDirection'])

        # Get EPI polarities and their metadata
        sdc_unwarp_wf = init_pepolar_unwarp_wf(
            matched_pe=matched_pe,
            omp_nthreads=omp_nthreads)
        sdc_unwarp_wf.inputs.inputnode.epi_pe_dir = epi_meta['PhaseEncodingDirection']
        sdc_unwarp_wf.inputs.inputnode.fmaps_epi = fmaps_epi

        workflow.connect([
            (inputnode, sdc_unwarp_wf, [
                ('epi_file', 'inputnode.in_reference'),
                ('epi_brain', 'inputnode.in_reference_brain'),
                ('epi_mask', 'inputnode.in_mask')]),
        ])

    # FIELDMAP path
    elif 'fieldmap' in fmaps or 'phasediff' in fmaps:
        from .fmap import init_fmap2field_wf
        from .unwarp import init_sdc_unwarp_wf

        # SyN works without this metadata
        if epi_meta.get('PhaseEncodingDirection') is None:
            raise ValueError(
                'PhaseEncodingDirection is not defined within the metadata retrieved '
                'for the intended EPI (DWI, BOLD, or SBRef) run.')

        if 'fieldmap' in fmaps:
            from .fmap import init_fmap_wf
            outputnode.inputs.method = 'FMB (fieldmap-based) - directly measured B0 map'
            fmap_wf = init_fmap_wf(
                omp_nthreads=omp_nthreads,
                fmap_bspline=False)
            # set inputs
            fmap_wf.inputs.inputnode.magnitude = [
                m for m, _ in fmaps['fieldmap']['magnitude']]
            fmap_wf.inputs.inputnode.fieldmap = [
                m for m, _ in fmaps['fieldmap']['fieldmap']]
        elif 'phasediff' in fmaps:
            from .phdiff import init_phdiff_wf
            outputnode.inputs.method = 'FMB (fieldmap-based) - phase-difference map'
            fmap_wf = init_phdiff_wf(omp_nthreads=omp_nthreads)
            # set inputs
            fmap_wf.inputs.inputnode.magnitude = [
                m for m, _ in fmaps['phasediff']['magnitude']]
            fmap_wf.inputs.inputnode.phasediff = fmaps['phasediff']['phases']

        fmap2field_wf = init_fmap2field_wf(omp_nthreads=omp_nthreads, debug=debug)
        fmap2field_wf.inputs.inputnode.metadata = epi_meta

        sdc_unwarp_wf = init_sdc_unwarp_wf(
            omp_nthreads=omp_nthreads,
            debug=debug,
            name='sdc_unwarp_wf')

        workflow.connect([
            (inputnode, fmap2field_wf, [
                ('epi_file', 'inputnode.in_reference'),
                ('epi_brain', 'inputnode.in_reference_brain')]),
            (inputnode, sdc_unwarp_wf, [
                ('epi_file', 'inputnode.in_reference'),
                ('epi_brain', 'inputnode.in_reference_brain')]),
            (fmap_wf, fmap2field_wf, [
                ('outputnode.fmap', 'inputnode.fmap'),
                ('outputnode.fmap_ref', 'inputnode.fmap_ref'),
                ('outputnode.fmap_mask', 'inputnode.fmap_mask')]),
            (fmap2field_wf, sdc_unwarp_wf, [
                ('outputnode.out_warp', 'inputnode.in_warp')]),

        ])
    elif not only_syn:
        raise ValueError('Fieldmaps of types %s are not supported' %
                         ', '.join(['"%s"' % f for f in fmaps]))

    # FIELDMAP-less path
    if 'syn' in fmaps:
        from .syn import init_syn_sdc_wf
        syn_sdc_wf = init_syn_sdc_wf(
            epi_pe=epi_meta.get('PhaseEncodingDirection', None),
            omp_nthreads=omp_nthreads)

        workflow.connect([
            (inputnode, syn_sdc_wf, [
                ('epi_file', 'inputnode.in_reference'),
                ('epi_brain', 'inputnode.in_reference_brain'),
                ('t1w_brain', 'inputnode.t1w_brain'),
                ('std2anat_xfm', 'inputnode.std2anat_xfm')]),
        ])

        # XXX Eliminate branch when forcing isn't an option
        if only_syn:  # No fieldmaps, but --use-syn
            outputnode.inputs.method = 'FLB ("fieldmap-less", SyN-based)'
            sdc_unwarp_wf = syn_sdc_wf
        else:  # --force-syn was called when other fieldmap was present
            sdc_unwarp_wf.__desc__ = None
            workflow.connect([
                (syn_sdc_wf, outputnode, [
                    ('outputnode.out_reference', 'syn_ref')]),
            ])

    workflow.connect([
        (sdc_unwarp_wf, outputnode, [
            ('outputnode.out_warp', 'out_warp'),
            ('outputnode.out_reference', 'epi_file'),
            ('outputnode.out_reference_brain', 'epi_brain'),
            ('outputnode.out_mask', 'epi_mask')]),
    ])

    return workflow
