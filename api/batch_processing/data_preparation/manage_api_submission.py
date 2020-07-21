"""
Semi-automated process for submitting and managing camera trap API tasks.

Terminology:
- taskgroup: a group of requests
    usually 1 Azure Blob Storage container = 1 taskgroup, but we can also
    specify individual folders inside a container to constitute a taskgroup
- request: an individual call to the Batch Processing API, also known as a task
- task: a request
"""
#%% Imports

import json
import ntpath
import os
import posixpath
import pprint
from urllib.parse import urlsplit, unquote
from typing import Any, Dict, List

import clipboard
import humanfriendly

import ai4e_azure_utils  # from ai4eutils
import path_utils  # from ai4eutils
import sas_blob_utils  # from ai4eutils

from api.batch_processing.data_preparation import prepare_api_submission
from api.batch_processing.postprocessing import combine_api_outputs
from api.batch_processing.postprocessing.postprocess_batch_results import (
    PostProcessingOptions,
    process_batch_results)

#%% Constants I set per taskgroup

### Required

storage_account_name = 'blah'
container_name = 'blah'
base_task_name = 'institution-20191215'
base_output_folder_name = r'f:\institution'

# Shared Access Signature (SAS) tokens for the Azure Blob Storage container.
# These should NOT start with a '?'.
# The read-only token is used for accessing images; the write-enabled token is
# used for writing file lists.
read_only_sas_token = 'st=2019-12...'
read_write_sas_token = 'st=2019-12...'

caller = 'caller'
ENDPOINT_BASE = 'http://blah.endpoint.com:6022/v3/camera-trap/detection-batch'

### Typically left as default

container_prefix = ''

# This is how we break the container up into multiple taskgroups, e.g., for
# separate surveys. The typical case is to do the whole container as a single
# taskgroup.
folder_names = [''] # ['folder1', 'folder2', 'folder3']

# This is only necessary if you will be performing postprocessing steps that
# don't yet support SAS URLs, specifically the "subsetting" step, or in some
# cases the splitting of files into multiple output directories for
# empty/animal/vehicle/people.
#
# For those applications, you will need to mount the container to a local drive.
# For this case I recommend using rclone whether you are on Windows or Linux;
# rclone is much easier than blobfuse for transient mounting.
#
# But most of the time, you can ignore this.
image_base = 'x:\\'

additional_task_args: Dict[str, Any] = {}

# Supported model_versions: '4', '3', '4_prelim'
#
# Also available at the /supported_model_versions and /default_model_version
# endpoints
#
# Unless you have any specific reason to set this to a non-default value, leave
# it at the default, which as of 2020.04.28 is MegaDetector 4.1
#
# additional_task_args = {"model_version":"4_prelim"}
#


#%% Derived variables, path setup

assert len(folder_names) != 0

TASK_STATUS_ENDPOINT_URL = f'{ENDPOINT_BASE}/task'
SUBMISSION_ENDPOINT_URL = f'{ENDPOINT_BASE}/request_detections'

read_only_sas_url = sas_blob_utils.build_azure_storage_uri(
    account=storage_account_name, container=container_name,
    sas_token=read_only_sas_token)
write_sas_url = sas_blob_utils.build_azure_storage_uri(
    account=storage_account_name, container=container_name,
    sas_token=read_write_sas_token)

# local folders
filename_base = os.path.join(base_output_folder_name, base_task_name)
raw_api_output_folder = os.path.join(filename_base, 'raw_api_outputs')
combined_api_output_folder = os.path.join(filename_base, 'combined_api_outputs')
postprocessing_output_folder = os.path.join(filename_base, 'postprocessing')

os.makedirs(filename_base, exist_ok=True)
os.makedirs(raw_api_output_folder, exist_ok=True)
os.makedirs(combined_api_output_folder, exist_ok=True)
os.makedirs(postprocessing_output_folder, exist_ok=True)

# Turn warnings into errors if more than this many images are missing
max_tolerable_missing_images = 20

# import clipboard; clipboard.copy(read_only_sas_url)
# configure mount point with rclone config
# rclone mount mountname: z:

# Not yet automated:
# - Mounting the image source (see comment above)
# - Submitting the tasks (code written below, but it doesn't really work)
# - Handling failed tasks/shards/images (though most of the code exists in
#     generate_resubmission_list)
# - Pushing the final results to shared storage and generating a SAS URL to
#     share with the collaborator
# - Pushing the previews to shared storage


#%% Support functions

def url_to_filename(url):
    """
    See: https://gist.github.com/zed/c2168b9c52b032b5fb7d
    """
    # scheme, netloc, path, query, fragment
    urlpath = urlsplit(url).path

    basename = posixpath.basename(unquote(urlpath))
    if (os.path.basename(basename) != basename or
            unquote(posixpath.basename(urlpath)) != basename):
        raise ValueError  # reject '%2f' or 'dir%5Cbasename.ext' on Windows

    return basename


#%% Enumerate blobs to files

# file_lists_by_folder will contain a list of local JSON file names,
# each JSON file contains a list of blob names corresponding to an API taskgroup
file_lists_by_folder = []

# folder_name = folder_names[0]
for folder_name in folder_names:
    clean_folder_name = path_utils.clean_filename(folder_name)
    json_filename = f'{base_task_name}_{clean_folder_name}_all.json'
    list_file = os.path.join(filename_base, json_filename)

    # If this is intended to be a folder, it needs to end in '/', otherwise
    # files that start with the same string will match too
    folder_name = folder_name.replace('\\', '/')
    if len(folder_name) > 0 and (not folder_name.endswith('/')):
        folder_name = folder_name + '/'
    prefix = container_prefix + folder_name
    file_list = ai4e_azure_utils.enumerate_blobs_to_file(
        output_file=list_file,
        account_name=storage_account_name,
        container_name=container_name,
        sas_token=read_only_sas_token,
        blob_prefix=prefix)
    assert all(path_utils.is_image_file(s) for s in file_list)
    file_lists_by_folder.append(list_file)

assert len(file_lists_by_folder) == len(folder_names)


#%% Divide images into chunks for each folder

# The JSON file at folder_chunks[i][j] corresponds to task j of taskgroup i
folder_chunks = []

# list_file = file_lists_by_folder[0]
for list_file in file_lists_by_folder:
    chunked_files, chunks = prepare_api_submission.divide_files_into_tasks(
        list_file)
    print('Divided images into files:')
    for i_fn, fn in enumerate(chunked_files):
        new_fn = chunked_files[i_fn].replace('__', '_').replace('_all', '')
        os.rename(fn, new_fn)
        chunked_files[i_fn] = new_fn
        print(fn, len(chunks[i_fn]))
    folder_chunks.append(chunked_files)

assert len(folder_chunks) == len(folder_names)


#%% Create taskgroups and tasks, and upload image lists to blob storage

task_names = set()
taskgroups: List[List[prepare_api_submission.Task]] = []

for i, taskgroup_json_paths in enumerate(folder_chunks):

    taskgroup = []
    for j, task_json_path in enumerate(taskgroup_json_paths):

        # periods not allowed in task names
        task_json_filename = ntpath.basename(task_json_path)
        task_json_filename_root = os.path.splitext(task_json_filename)[0]
        task_name = f'{base_task_name}_{task_json_filename_root}'.replace(
            '.', '_')
        assert task_name not in task_names
        task_names.add(task_name)
        task = prepare_api_submission.Task(
            name=task_name, images_list_path=task_json_path, local=True)

        blob_name = f'api_inputs/{base_task_name}/{task_json_filename}'
        print(f'Task {task_name}: uploading {task_json_path} to {blob_name}')
        task.upload_images_list(
            account=storage_account_name, container=container_name,
            sas_token=read_write_sas_token, blob_name=blob_name)

        taskgroup.append(task)

    taskgroups.append(taskgroup)

assert len(taskgroups) == len(folder_names)


#%% Generate API calls for each task

request_strings = []

for taskgroup in taskgroups:
    for task in taskgroup:
        request = task.generate_api_request(
            caller=caller,
            input_container_url=read_only_sas_url,
            image_path_prefix=None,
            **additional_task_args)
        request_str = json.dumps(request, indent=1)
        request_strings.append(request_str)

pprint.pprint(request_strings)

clipboard.copy('\n\n'.join(request_strings))


#%% Run the tasks (still in progress, doesn't actually work yet)

# Not working yet, something is wrong with my post call

for taskgroup in taskgroups:
    for task in taskgroup:
        task_id = task.submit(SUBMISSION_ENDPOINT_URL)
        print(task.name, task_id)


#%% Estimate total time

n_images = 0
for fn in file_lists_by_folder:
    with open(fn, 'r') as f:
        images = json.load(f)
    n_images += len(images)

print(f'Processing a total of {n_images} images')

# Around 0.8s/image on 16 GPUs
expected_seconds = (0.8 / 16) * n_images
print(f'Expected time: {humanfriendly.format_timespan(expected_seconds)}')


#%% Manually define task groups if we ran the tasks manually

# The nested lists will make sense below, I promise.

# For just one task...
taskgroup_ids = [["9999"]]

# For multiple tasks...
taskgroup_ids = [["1111"], ["2222"], ["3333"]]

for i, taskgroup in enumerate(taskgroups):
    for j, task in enumerate(taskgroup):
        task.id = taskgroup_ids[i][j]


#%% Status check

for taskgroup in taskgroups:
    for task in taskgroup:
        response = task.check_status(TASK_STATUS_ENDPOINT_URL)
        print(response)


#%% Look for failed shards or missing images, start new tasks if necessary

n_resubmissions = 0
resubmitted_tasks = []

# i_taskgroup = 0; taskgroup = taskgroups[i_taskgroup]; task_id = taskgroup[0]
for i_taskgroup, taskgroup in enumerate(taskgroups):

    tasks = list(taskgroup)  # make a copy, because we append to taskgroup
    for task in tasks:

        response = task.check_status(TASK_STATUS_ENDPOINT_URL)

        n_failed_shards = response['Status']['message']['num_failed_shards']
        if n_failed_shards != 0:
            print(f'Warning: {n_failed_shards} failed shards for task '
                  f'{task.id}')

        output_file_urls = response['Status']['message']['output_file_urls']
        detections_url = output_file_urls['detections']
        fn = url_to_filename(detections_url)

        # Each taskgroup corresponds to one of our folders
        folder_name = folder_names[i_taskgroup]
        clean_folder_name = prepare_api_submission.clean_request_name(
            folder_name)
        assert (folder_name in fn) or (clean_folder_name in fn)
        assert 'chunk' in fn

        missing_images_fn = os.path.join(
            raw_api_output_folder, fn.replace('.json', '_missing.json'))

        missing_imgs = task.get_missing_images(verbose=True)
        ai4e_azure_utils.write_list_to_file(missing_images_fn, missing_imgs)
        num_missing_imgs = len(missing_imgs)
        if num_missing_imgs < max_tolerable_missing_images:
            continue

        print(f'Warning: {missing_imgs} missing images for task {task.id}')
        task_name = f'{base_task_name}_{folder_name}_{task.id}_missing_images'
        blob_name = f'api_inputs/{base_task_name}/{task_name}.json'
        new_task = prepare_api_submission.Task(
            name=task_name, images_list_path=missing_images_fn, local=True)
        print(f'Task {task_name}: uploading {missing_images_fn} to {blob_name}')
        new_task.upload_images_list(
            account=storage_account_name, container=container_name,
            blob_name=blob_name, sas_token=read_write_sas_token)
        request = new_task.generate_api_request(
            caller=caller, input_container_url=read_only_sas_url,
            image_path_prefix=None, **additional_task_args)

        taskgroup.append(new_task)
        resubmitted_tasks.append(new_task)

        # automatic submission
        # new_task.submit(SUBMISSION_ENDPOINT_URL)

        # manual submission
        print(f'\nResbumission task for {task_id}:\n')
        print(json.dumps(request, indent=1))

        n_resubmissions += 1

    # ...for each task

# ...for each task group

if n_resubmissions == 0:
    print('No resubmissions necessary')


#%% Resubmit tasks for failed shards, add to appropriate task groups

if False:

    #%%
    for task in resubmitted_tasks:
        response = task.check_status(TASK_STATUS_ENDPOINT_URL)
        print(response)

    taskgroup_ids = [['2233', '9484', '1222'], ['1197', '1702', '2764']]

    for i, taskgroup in enumerate(taskgroups):
        for j, task in enumerate(taskgroup):
            if hasattr(task, 'id'):
                assert task.id == taskgroup_ids[i][j]
            else:
                task.id = taskgroup_ids[i][j]


#%% Pull results

task_id_to_results_file = {}

# i_taskgroup = 0; taskgroup = taskgroups[i_taskgroup]; task_id = taskgroup[0]
for i_taskgroup, taskgroup in enumerate(taskgroups):

    for task in taskgroup:

        response = task.check_status(TASK_STATUS_ENDPOINT_URL)

        output_file_urls = response['Status']['message']['output_file_urls']
        detections_url = output_file_urls['detections']
        fn = url_to_filename(detections_url)

        # n_failed_shards = response['status']['message']['num_failed_shards']
        # assert n_failed_shards == 0

        # Each taskgroup corresponds to one of our folders
        folder_name = folder_names[i_taskgroup]
        clean_folder_name = prepare_api_submission.clean_request_name(
            folder_name)
        assert (folder_name in fn) or (clean_folder_name in fn)
        assert 'chunk' in fn or 'missing' in fn

        output_file = os.path.join(raw_api_output_folder, fn)
        prepare_api_submission.download_url(detections_url, output_file)
        task_id_to_results_file[task.id] = output_file

    # ...for each task

# ...for each task group


#%% Combine results from task groups into final output files

folder_name_to_combined_output_file = {}

for i_taskgroup, taskgroup in enumerate(taskgroups):

    folder_name_raw = folder_names[i_taskgroup]
    folder_name = path_utils.clean_filename(folder_name_raw)
    print(f'Combining results for {folder_name}')

    results_files = []
    for task in taskgroup:
        raw_output_file = task_id_to_results_file[task.id]
        results_files.append(raw_output_file)

    combined_api_output_file = os.path.join(
        combined_api_output_folder,
        f'{base_task_name}{folder_name}_detections.json')

    print(f'Combining the following into {combined_api_output_file}')
    pprint.pprint(results_files)

    combine_api_outputs.combine_api_output_files(
        results_files, combined_api_output_file)
    folder_name_to_combined_output_file[folder_name] = combined_api_output_file

    # Check that we have (almost) all the images
    list_file = file_lists_by_folder[i_taskgroup]
    with open(list_file, 'r') as f:
        requested_images_set = set(json.load(f))
    with open(combined_api_output_file, 'r') as f:
        results = json.load(f)
        result_images_set = set(im['file'] for im in results['images'])
    missing_files = requested_images_set - result_images_set
    missing_images = path_utils.find_image_strings(missing_files)
    if len(missing_images) > 0:
        print(f'Warning: {len(missing_images)} missing images for folder '
              f'[{folder_name}]')
    assert len(missing_images) < max_tolerable_missing_images

    # Something has gone bonkers if there are images in the results that
    # aren't in the request
    extra_images = result_images_set - requested_images_set
    assert len(extra_images) == 0

# ...for each folder


#%% Post-processing (no ground truth)

html_output_files = []

# i_folder = 0; folder_name_raw = folder_names[i_folder]
for i_folder, folder_name_raw in enumerate(folder_names):

    options = PostProcessingOptions()
    options.image_base_dir = read_only_sas_url
    options.parallelize_rendering = True
    options.include_almost_detections = True
    options.num_images_to_sample = 5000
    options.confidence_threshold = 0.8
    options.almost_detection_confidence_threshold = options.confidence_threshold - 0.05
    options.ground_truth_json_file = None
    options.separate_detections_by_category = True

    folder_name = path_utils.clean_filename(folder_name_raw)
    if len(folder_name) == 0:
        folder_token = ''
    else:
        folder_token = folder_name + '_'
    output_base = os.path.join(postprocessing_output_folder, folder_token + \
        base_task_name + '_{:.3f}'.format(options.confidence_threshold))
    os.makedirs(output_base, exist_ok=True)
    print('Processing {} to {}'.format(folder_name, output_base))
    api_output_file = folder_name_to_combined_output_file[folder_name]

    options.api_output_file = api_output_file
    options.output_dir = output_base
    ppresults = process_batch_results(options)
    html_output_files.append(ppresults.output_html_file)

for fn in html_output_files:
    os.startfile(fn)


#%% Manual processing follows

#
# Everything after this should be considered mostly manual, and no longer includes
# looping over folders.
#


#%% Repeat detection elimination, phase 1

# Deliberately leaving these imports here, rather than at the top, because this cell is not
# typically executed
from api.batch_processing.postprocessing.repeat_detection_elimination import repeat_detections_core
import path_utils
task_index = 0

options = repeat_detections_core.RepeatDetectionOptions()

options.confidenceMin = 0.6
options.confidenceMax = 1.01
options.iouThreshold = 0.85
options.occurrenceThreshold = 10
options.maxSuspiciousDetectionSize = 0.2

options.bRenderHtml = False
options.imageBase = read_only_sas_url
rde_string = 'rde_{:.2f}_{:.2f}_{}_{:.2f}'.format(
    options.confidenceMin, options.iouThreshold,
    options.occurrenceThreshold, options.maxSuspiciousDetectionSize)
options.outputBase = os.path.join(filename_base, rde_string)
options.filenameReplacements = {'':''}

options.debugMaxDir = -1
options.debugMaxRenderDir = -1
options.debugMaxRenderDetection = -1
options.debugMaxRenderInstance = -1

api_output_filename = list(folder_name_to_combined_output_file.values())[task_index]
filtered_output_filename = path_utils.insert_before_extension(api_output_filename, 'filtered_{}'.format(rde_string))

suspiciousDetectionResults = repeat_detections_core.find_repeat_detections(api_output_filename,
                                                                           None,
                                                                           options)

clipboard.copy(os.path.dirname(suspiciousDetectionResults.filterFile))


#%% Manual RDE step

## DELETE THE ANIMALS ##


#%% Re-filtering

from api.batch_processing.postprocessing.repeat_detection_elimination import remove_repeat_detections

remove_repeat_detections.remove_repeat_detections(
    inputFile=api_output_filename,
    outputFile=filtered_output_filename,
    filteringDir=os.path.dirname(suspiciousDetectionResults.filterFile),
    options=options
    )


#%% Post-processing (post-RDE)

html_output_files = []

# i_folder = 0; folder_name_raw = folder_names[i_folder]
for i_folder, folder_name_raw in enumerate(folder_names):

    options = PostProcessingOptions()
    options.image_base_dir = read_only_sas_url
    options.parallelize_rendering = True
    options.include_almost_detections = True
    options.num_images_to_sample = 5000
    options.confidence_threshold = 0.8
    options.almost_detection_confidence_threshold = options.confidence_threshold - 0.05
    options.ground_truth_json_file = None
    options.separate_detections_by_category = True

    folder_name = path_utils.clean_filename(folder_name_raw)
    if len(folder_name) == 0:
        folder_token = ''
    else:
        folder_token = folder_name + '_'
    output_base = os.path.join(postprocessing_output_folder, folder_token + \
        base_task_name + '_{}_{:.3f}'.format(rde_string, options.confidence_threshold))
    os.makedirs(output_base, exist_ok=True)
    print('Processing {} to {}'.format(folder_name, output_base))
    # api_output_file = folder_name_to_combined_output_file[folder_name]

    options.api_output_file = filtered_output_filename
    options.output_dir = output_base
    ppresults = process_batch_results(options)
    html_output_files.append(ppresults.output_html_file)

for fn in html_output_files:
    os.startfile(fn)


#%% Subsetting

data = None

from api.batch_processing.postprocessing.subset_json_detector_output import subset_json_detector_output
from api.batch_processing.postprocessing.subset_json_detector_output import SubsetJsonDetectorOutputOptions

input_filename = inputFilename = list(folder_name_to_combined_output_file.values())[0]
output_base = os.path.join(filename_base, 'json_subsets')

folders = os.listdir(image_base)

if data is None:
    with open(input_filename) as f:
        data = json.load(f)

print('Data set contains {} images'.format(len(data['images'])))

# i_folder = 0; folder_name = folders[i_folder]
for i_folder, folder_name in enumerate(folders):

    output_filename = os.path.join(output_base, folder_name + '.json')
    print('Processing folder {} of {} ({}) to {}'.format(i_folder, len(folders), folder_name,
          output_filename))

    options = SubsetJsonDetectorOutputOptions()
    options.confidence_threshold = 0.4
    options.overwrite_json_files = True
    options.make_folder_relative = True
    options.query = folder_name + '\\'

    subset_data = subset_json_detector_output(input_filename, output_filename, options, data)


#%% Folder splitting

from api.batch_processing.postprocessing.separate_detections_into_folders import separate_detections_into_folders
from api.batch_processing.postprocessing.separate_detections_into_folders import SeparateDetectionsIntoFoldersOptions

default_threshold = 0.8
options = SeparateDetectionsIntoFoldersOptions()

options.results_file = r"blah-20200629_detections.json"
options.base_input_folder = "z:\\"
options.base_output_folder = r"E:\blah-out"
options.n_threads = 100
options.allow_existing_directory = False

separate_detections_into_folders(options)
