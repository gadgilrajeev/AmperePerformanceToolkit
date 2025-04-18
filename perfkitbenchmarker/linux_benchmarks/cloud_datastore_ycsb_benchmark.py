# Copyright 2015 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run YCSB benchmark against Google Cloud Datastore.

Before running this benchmark, you have to download your P12
service account private key file to local machine, and pass the path
via 'google_datastore_keyfile' parameters to PKB.

Service Account email associated with the key file is also needed to
pass to PKB.

By default, this benchmark provision 1 single-CPU VM and spawn 1 thread
to test Datastore.
"""

import concurrent.futures
import logging
from multiprocessing import pool as pool_lib
import time

from absl import flags
from google.cloud import datastore
from google.oauth2 import service_account
from perfkitbenchmarker import background_tasks
from perfkitbenchmarker import configs
from perfkitbenchmarker import errors
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.linux_packages import ycsb


BENCHMARK_NAME = 'cloud_datastore_ycsb'
BENCHMARK_CONFIG = """
cloud_datastore_ycsb:
  description: >
      Run YCSB agains Google Cloud Datastore.
      Configure the number of VMs via --num-vms.
  vm_groups:
    default:
      os_type: ubuntu2204  # Python 2
      vm_spec: *default_single_core
      vm_count: 1
  flags:
    openjdk_version: 11
    gcloud_scopes: >
      trace
      datastore
      cloud-platform"""

# the name of the database entity created when running datastore YCSB
# https://github.com/brianfrankcooper/YCSB/tree/master/googledatastore
_YCSB_COLLECTIONS = ['usertable']

# constants used for cleaning up database
_CLEANUP_THREAD_POOL_WORKERS = 30
_CLEANUP_KIND_READ_BATCH_SIZE = 12000
_CLEANUP_KIND_DELETE_BATCH_SIZE = 6000
_CLEANUP_KIND_DELETE_PER_THREAD_BATCH_SIZE = 3000
_CLEANUP_KIND_DELETE_OP_BATCH_SIZE = 500

FLAGS = flags.FLAGS
_KEYFILE = flags.DEFINE_string(
    'google_datastore_keyfile',
    None,
    'The path to Google API JSON private key file',
)
_PROJECT_ID = flags.DEFINE_string(
    'google_datastore_projectId',
    None,
    'The project ID that has Cloud Datastore service',
)
_DATASET_ID = flags.DEFINE_string(
    'google_datastore_datasetId',
    None,
    'The database ID that has Cloud Datastore service',
)
_DEBUG = flags.DEFINE_string(
    'google_datastore_debug', 'false', 'The logging level when running YCSB'
)
_REPOPULATE = flags.DEFINE_boolean(
    'google_datastore_repopulate',
    False,
    'If True, empty database & repopulate with new data.'
    'By default, tests are run with pre-populated data.',
)
_TARGET_LOAD_QPS = flags.DEFINE_integer(
    'google_datastore_target_load_qps',
    500,
    'The target QPS to load the database at. See'
    ' https://cloud.google.com/datastore/docs/best-practices#ramping_up_traffic'
    ' for more info.',
)

_KEYFILE_LOCAL_PATH = '/tmp/key.json'
_INSERTION_RETRY_LIMIT = 100
_SLEEP_AFTER_LOADING_SECS = 30 * 60


def GetConfig(user_config):
  config = configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)
  if FLAGS['ycsb_client_vms'].present:
    config['vm_groups']['default']['vm_count'] = FLAGS.ycsb_client_vms
  return config


def CheckPrerequisites(_):
  if not ycsb.SKIP_LOAD_STAGE.value and not _TARGET_LOAD_QPS.value:
    raise errors.Setup.InvalidFlagConfigurationError(
        '--google_datastore_target_load_qps must be set when loading the'
        ' database.'
    )


def _Install(vm):
  """Installs YCSB benchmark & copies datastore keyfile to client vm."""
  vm.Install('ycsb')

  # Copy private key file to VM
  if _KEYFILE.value:
    if _KEYFILE.value.startswith('gs://'):
      vm.Install('google_cloud_sdk')
      vm.RemoteCommand(f'gsutil cp {_KEYFILE.value} {_KEYFILE_LOCAL_PATH}')
    else:
      vm.RemoteCopy(_KEYFILE.value, _KEYFILE_LOCAL_PATH)


def _GetCommonYcsbArgs():
  """Returns common YCSB args."""
  args = {
      'googledatastore.projectId': _PROJECT_ID.value,
      'googledatastore.debug': _DEBUG.value,
  }
  # if not provided, use the (default) database.
  if _DATASET_ID.value:
    args['googledatastore.datasetId'] = _DATASET_ID.value
  return args


def _GetYcsbExecutor():
  env = {}
  if _KEYFILE.value:
    env = {'GOOGLE_APPLICATION_CREDENTIALS': _KEYFILE_LOCAL_PATH}
  return ycsb.YCSBExecutor('googledatastore', environment=env)


def RampUpLoad(
    ycsb_executor: ycsb.YCSBExecutor,
    vms: list[virtual_machine.VirtualMachine],
    load_kwargs: dict[str, str] = None,
) -> None:
  """Loads YCSB by gradually incrementing target QPS.

  Note that this requires clients to be overprovisioned, as the target QPS
  for YCSB is generally a "throttling" mechanism where the threads try to send
  as much QPS as possible and then get throttled. If clients are
  underprovisioned then it's possible for the run to not hit the desired
  target, which may be undesired behavior.

  See
  https://cloud.google.com/datastore/docs/best-practices#ramping_up_traffic
  for an example of why this is needed.

  Args:
    ycsb_executor: The YCSB executor to use.
    vms: The client VMs to generate the load.
    load_kwargs: Extra run arguments.
  """
  target_load_qps = _TARGET_LOAD_QPS.value
  incremental_targets = ycsb_executor.GetIncrementalQpsTargets(target_load_qps)
  logging.info('Incremental load stage target QPS: %s', incremental_targets)

  ramp_up_args = load_kwargs.copy()
  for target in incremental_targets:
    target /= len(vms)
    ramp_up_args['target'] = int(target)
    ramp_up_args['threads'] = min(FLAGS.ycsb_preload_threads, int(target))
    ramp_up_args['maxexecutiontime'] = ycsb.INCREMENTAL_TIMELIMIT_SEC
    ycsb_executor.Load(vms, load_kwargs=ramp_up_args)

  target_load_qps /= len(vms)
  load_kwargs['target'] = int(target_load_qps)
  ycsb_executor.Load(vms, load_kwargs=load_kwargs)


def Prepare(benchmark_spec):
  """Prepare the virtual machines to run cloud datastore.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.
  """
  if _REPOPULATE.value:
    EmptyDatabase()

  vms = benchmark_spec.vms

  # Install required packages and copy credential files
  background_tasks.RunThreaded(_Install, vms)

  if ycsb.SKIP_LOAD_STAGE.value:
    return

  load_kwargs = _GetCommonYcsbArgs()
  load_kwargs['core_workload_insertion_retry_limit'] = _INSERTION_RETRY_LIMIT
  executor = _GetYcsbExecutor()
  RampUpLoad(executor, vms, load_kwargs)


def Run(benchmark_spec):
  """Spawn YCSB and gather the results.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.

  Returns:
    A list of sample.Sample instances.
  """
  if _REPOPULATE.value and not FLAGS.ycsb_skip_run_stage:
    logging.info('Sleeping 30 minutes to allow for compaction.')
    time.sleep(_SLEEP_AFTER_LOADING_SECS)

  executor = _GetYcsbExecutor()
  vms = benchmark_spec.vms
  run_kwargs = _GetCommonYcsbArgs()
  run_kwargs.update({
      'googledatastore.tracingenabled': True,
      'readallfields': True,
      'writeallfields': True,
  })
  samples = list(executor.Run(vms, run_kwargs=run_kwargs))
  return samples


def Cleanup(_):
  pass


def EmptyDatabase():
  """Deletes all entries in a datastore database."""
  if _KEYFILE.value:
    dataset_id = _DATASET_ID.value
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=_CLEANUP_THREAD_POOL_WORKERS
    )

    logging.info('Attempting to delete all data in %s', dataset_id)

    credentials = GetDatastoreDeleteCredentials()

    futures = []
    for kind in _YCSB_COLLECTIONS:
      futures.append(
          executor.submit(
              _ReadAndDeleteAllEntities(dataset_id, credentials, kind)
          )
      )

    concurrent.futures.wait(
        futures, timeout=None, return_when=concurrent.futures.ALL_COMPLETED
    )
    logging.info('Deleted all data for %s', dataset_id)

  else:
    logging.warning('Manually delete all the entries via GCP portal.')


def GetDatastoreDeleteCredentials():
  """Returns credentials to datastore db."""
  if _KEYFILE.value is not None and _KEYFILE.value.startswith('gs://'):
    # Copy private keyfile to local disk
    cp_cmd = [
        'gsutil',
        'cp',
        _KEYFILE.value,
        _KEYFILE_LOCAL_PATH,
    ]
    vm_util.IssueCommand(cp_cmd)
    credentials_path = _KEYFILE_LOCAL_PATH
  else:
    credentials_path = _KEYFILE.value

  if credentials_path is None:
    raise errors.Benchmarks.RunError('Credentials path is None')

  credentials = service_account.Credentials.from_service_account_file(
      credentials_path,
      scopes=datastore.client.Client.SCOPE,
  )

  return credentials


def _ReadAndDeleteAllEntities(dataset_id, credentials, kind):
  """Reads and deletes all kind entries in a datastore database.

  Args:
    dataset_id: Cloud Datastore client dataset id.
    credentials: Cloud Datastore client credentials.
    kind: Kind for which entities will be deleted.

  Raises:
    ValueError: In case of delete failures.
  """
  task_id = 1
  start_cursor = None
  pool = pool_lib.ThreadPool(processes=_CLEANUP_THREAD_POOL_WORKERS)

  # We use a cursor to fetch entities in larger read batches and submit delete
  # tasks to delete them in smaller delete batches (500 at a time) due to
  # datastore single operation restriction.
  entity_read_count = 0
  total_entity_count = 0
  delete_keys = []
  while True:
    query = _CreateClient(dataset_id, credentials).query(kind=kind)
    query.keys_only()
    query_iter = query.fetch(
        start_cursor=start_cursor, limit=_CLEANUP_KIND_READ_BATCH_SIZE
    )

    for current_entities in query_iter.pages:
      delete_keys.extend([entity.key for entity in current_entities])
      entity_read_count = len(delete_keys)
      # logging.debug('next batch of entities for %s - total = %d', kind,
      #              entity_read_count)
      if entity_read_count >= _CLEANUP_KIND_DELETE_BATCH_SIZE:
        total_entity_count += entity_read_count
        logging.info('Creating tasks...Read %d in total', total_entity_count)
        while delete_keys:
          delete_chunk = delete_keys[
              :_CLEANUP_KIND_DELETE_PER_THREAD_BATCH_SIZE
          ]
          delete_keys = delete_keys[_CLEANUP_KIND_DELETE_PER_THREAD_BATCH_SIZE:]
          # logging.debug(
          #    'Creating new Task %d - Read %d entities for %s kind , Read %d '
          #    + 'in total.',
          #    task_id, entity_read_count, kind, total_entity_count)
          deletion_task = _DeletionTask(kind, task_id)
          pool.apply_async(
              deletion_task.DeleteEntities,
              (
                  dataset_id,
                  credentials,
                  delete_chunk,
              ),
          )
          task_id += 1

        # Reset delete batch,
        entity_read_count = 0
        delete_keys = []

    # Read this after the pages are retrieved otherwise it will be set to None.
    start_cursor = query_iter.next_page_token
    if start_cursor is None:
      logging.info('Read all existing records for %s', kind)
      if delete_keys:
        logging.info(
            'Entities batch is not empty %d, submitting new tasks',
            len(delete_keys),
        )
        while delete_keys:
          delete_chunk = delete_keys[
              :_CLEANUP_KIND_DELETE_PER_THREAD_BATCH_SIZE
          ]
          delete_keys = delete_keys[_CLEANUP_KIND_DELETE_PER_THREAD_BATCH_SIZE:]
          logging.debug(
              (
                  'Creating new Task %d - Read %d entities for %s kind , Read'
                  ' %d in total.'
              ),
              task_id,
              entity_read_count,
              kind,
              total_entity_count,
          )
          deletion_task = _DeletionTask(kind, task_id)
          pool.apply_async(
              deletion_task.DeleteEntities,
              (
                  dataset_id,
                  credentials,
                  delete_chunk,
              ),
          )
          task_id += 1
      break

  logging.info('Waiting for all tasks - %d to complete...', task_id)
  time.sleep(60)
  pool.close()
  pool.join()

  # Rerun the query and delete any leftovers to make sure that all records
  # are deleted as intended.
  client = _CreateClient(dataset_id, credentials)
  query = client.query(kind=kind)
  query.keys_only()
  entities = list(query.fetch(limit=20000))
  if entities:
    logging.info('Deleting leftover %d entities for %s', len(entities), kind)
    total_entity_count += len(entities)
    deletion_task = _DeletionTask(kind, task_id)
    delete_keys = []
    delete_keys.extend([entity.key for entity in entities])
    deletion_task.DeleteEntities(dataset_id, credentials, delete_keys)

  logging.info(
      'Deleted all data for %s - %s - %d', dataset_id, kind, total_entity_count
  )


def _CreateClient(dataset_id, credentials):
  """Creates a datastore client for the dataset using the credentials.

  Args:
    dataset_id: Cloud Datastore client dataset id.
    credentials: Cloud Datastore client credentials.

  Returns:
    Datastore client.
  """
  return datastore.Client(project=dataset_id, credentials=credentials)


class _DeletionTask:
  """Represents a cleanup deletion task.

  Attributes:
    kind: Datastore kind to be deleted.
    task_id: Task id
    entity_deletion_count: No of entities deleted.
    deletion_error: Set to true if deletion fails with an error.
  """

  def __init__(self, kind, task_id):
    self.kind = kind
    self.task_id = task_id
    self.entity_deletion_count = 0
    self.deletion_error = False

  def DeleteEntities(self, dataset_id, credentials, delete_entities):
    """Deletes entities in a datastore database in batches.

    Args:
      dataset_id: Cloud Datastore client dataset id.
      credentials: Cloud Datastore client credentials.
      delete_entities: Entities to delete.

    Returns:
      number of records deleted.
    Raises:
      ValueError: In case of delete failures.
    """
    try:
      client = datastore.Client(project=dataset_id, credentials=credentials)
      logging.info('Task %d - Started deletion for %s', self.task_id, self.kind)
      while delete_entities:
        chunk = delete_entities[:_CLEANUP_KIND_DELETE_OP_BATCH_SIZE]
        delete_entities = delete_entities[_CLEANUP_KIND_DELETE_OP_BATCH_SIZE:]
        client.delete_multi(chunk)
        self.entity_deletion_count += len(chunk)

      logging.info(
          'Task %d - Completed deletion for %s - %d',
          self.task_id,
          self.kind,
          self.entity_deletion_count,
      )
      return self.entity_deletion_count
    except ValueError as error:
      logging.exception(
          'Task %d - Delete entities for %s failed due to %s',
          self.task_id,
          self.kind,
          error,
      )
      self.deletion_error = True
      raise error
