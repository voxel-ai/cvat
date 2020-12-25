#from dataclasses import dataclass
from abc import ABC, abstractmethod, abstractproperty
from io import BytesIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import WaiterError

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import PublicAccess

from cvat.apps.engine.log import slogger
from cvat.apps.engine.models import CredentialsTypeChoice, CloudProviderChoice

class CloudStorage(ABC):

    def __init__(self):
        self._files = []

    @abstractproperty
    def name(self):
        pass

    @abstractmethod
    def create(self):
        pass

    @abstractmethod
    def is_exist(self):
        pass

    # @abstractmethod
    # def head(self):
    #     pass

    # @abstractproperty
    # def supported_files(self):
    #    pass

    @abstractmethod
    def initialize_content(self):
        pass

    @abstractmethod
    def download_fileobj(self, key):
        pass

    def download_file(self, key, path):
        file_obj = self.download_fileobj(key)
        if isinstance(file_obj, BytesIO):
            with open(path, 'wb') as f:
                f.write(file_obj.getvalue())

    @abstractmethod
    def upload_file(self, file_obj, file_name):
        pass

    def __contains__(self, file_name):
        return file_name in (item['name'] for item in self._files.values())

    def __len__(self):
        return len(self._files)

    @property
    def content(self):
        return map(lambda x: x['name'] , self._files)

def get_cloud_storage_instance(cloud_provider, **details):
    instance = None
    if cloud_provider == CloudProviderChoice.AWS_S3:
        instance = AWS_S3(
            bucket=details.get('resource_name'),
            session_token=details.get('session_token'),
            key_id=details.get('key'),
            secret_key=details.get('secret_key')
        )
    elif cloud_provider == CloudProviderChoice.AZURE_CONTAINER:
        instance = AzureBlobContainer(
            container_name=details.get('resource_name'),
            sas_token=details.get('session_token'),
            accounr_name=details.get('key'),
            account_access_key=details.get('secret_key')
        )
    return instance

class AWS_S3(CloudStorage):
    def __init__(self, **kwargs):
        super().__init__()
        assert (bucket_name := kwargs.get('bucket')), 'Bucket name was not found'
        self._bucket_name = bucket_name

        key_id, secret_key = None, None
        if (session_token := kwargs.get('session_token')):
            assert (key_id := kwargs.get('key_id')), 'Key id was not found'
            assert (secret_key := kwargs.get('secret_key')), 'Secret key was not found'

        self._client_s3 = boto3.client(
            's3',
            aws_access_key_id=key_id,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token
        )

        self._s3 = boto3.resource('s3')
        self._bucket = self._s3.Bucket(bucket_name)

    @property
    def bucket(self):
        return self._bucket

    @property
    def name(self):
        return self._bucket_name

    # def is_object_exist(self, verifiable='bucket_exist', config=None):
    #     waiter = self._client_s3.get_waiter(verifiable)
    #     waiter.wait(**config)

    def is_exist(self):
        waiter = self._client_s3.get_waiter('bucket_exists')
        try:
            waiter.wait(
                Bucket=self._bucket_name,
                WaiterConfig={
                    'Delay': 5, # The amount of time in seconds to wait between attempts. Default: 5
                    'MaxAttempts': 3 # The maximum number of attempts to be made. Default: 20
                }
            )
        except WaiterError:
            raise Exception('A resource {} unavailable'.format(self._bucket_name))

    def is_object_exist(self, key_object):
        waiter = self._client_s3.get_waiter('object_exists')
        try:
            waiter.wait(
                Bucket=self._bucket,
                Key=key_object,
                WaiterConfig={
                    'Delay': 5,
                    'MaxAttempts': 3,
                },
            )
        except WaiterError:
            raise Exception('A file {} unavailable'.format(key_object))

    def head(self):
        pass

    # @property
    # def supported_files(self):
    #     pass

    def upload_file(self, file_obj, file_name):
        self._bucket.upload_fileobj(
            Fileobj=file_obj,
            Key=file_name,
            Config=TransferConfig(max_io_queue=10)
        )

    def initialize_content(self):
        files = self._bucket.objects.all()
        self._files = [{
            'name': item.key,
        } for item in files]

    def download_fileobj(self, key):
        buf = BytesIO()
        self.bucket.download_fileobj(
            Key=key,
            Fileobj=buf,
            Config=TransferConfig(max_io_queue=10)
        )
        buf.seek(0)
        return buf

    def create(self):
        try:
            _ = self._bucket.create(
                ACL='private',
                CreateBucketConfiguration={
                    'LocationConstraint': 'us-east-2',#TODO
                },
                ObjectLockEnabledForBucket=False
            )
        except Exception as ex:#botocore.errorfactory.BucketAlreadyExists
            msg = str(ex)
            slogger.glob.info(msg)
            raise Exception(str(ex))

class AzureBlobContainer(CloudStorage):

    def __init__(self, **kwargs):
        super().__init__()
        assert (container_name := kwargs.get('container_name')), 'Container name was not found'
        assert (account_name := kwargs.get('account_name')), 'Account name was not found'
        assert (credentials := kwargs.get('sas_token') if kwargs.get('sas_token') else kwargs.get('account_access_key')), 'Credentials were not granted'

        self._blob_service_client = BlobServiceClient(account_url=self.account_url, credential=credentials)
        self._container_client = self._blob_service_client.get_container_client(container_name)

        self._account_name = account_name

    @property
    def container(self):
        return self._container_client

    @property
    def name(self):
        return self._container_client.container_name

    @property
    def account_url(self):
        return "{}.blob.core.windows.net".format(self._account_name)

    def create(self):
        try:
            self._container_client.create_container(
               metadata={
                   'type' : 'created by CVAT',
               },
               public_access=PublicAccess.OFF
            )
        except ResourceExistsError:
            msg = f"{self._container_client.container_name} alredy exists"
            slogger.glob.info(msg)
            raise Exception(msg)

    def is_exist(self):
        try:
            self._container_client.create_container()
            self._container_client.delete_container()
            return False
        except ResourceExistsError:
            return True

    def is_object_exist(self, file_name):
        blob_client = self._container_client.get_blob_client(file_name)
        return blob_client.exists()

    def head(self):
        pass

    # @property
    # def supported_files(self):
    #     pass

    def upload_file(self, file_obj, file_name):
        self._container_client.upload_blob(name=file_name, data=file_obj)


    # def multipart_upload(self, file_obj):
    #     pass

    def initialize_content(self):
        files = self._container_client.list_blobs()
        self._files = [{
            'name': item.name
        } for item in files]

    def download_fileobj(self, key):
        MAX_CONCURRENCY = 3
        buf = BytesIO()
        storage_stream_downloader = self._container_client.download_blob(
            blob=key,
            offset=None,
            length=None,
        )
        storage_stream_downloader.download_to_stream(buf, max_concurrency=MAX_CONCURRENCY)
        buf.seek(0)
        return buf

class GOOGLE_DRIVE(CloudStorage):
    pass

class Credentials:
    __slots__ = ('key', 'secret_key', 'session_token', 'credentials_type')

    def __init__(self, **credentials):
        self.key = credentials.get('key', '')
        self.secret_key = credentials.get('secret_key', '')
        self.session_token = credentials.get('session_token', '')
        self.credentials_type = credentials.get('credentials_type', None)

    def convert_to_db(self):
        converted_credentials = {
            CredentialsTypeChoice.TOKEN : self.session_token,
            CredentialsTypeChoice.KEY_TOKEN_PAIR : " ".join([self.key, self.session_token]),
            CredentialsTypeChoice.KEY_SECRET_KEY_PAIR : " ".join([self.key, self.secret_key])
        }
        return converted_credentials[self.credentials_type]

    def convert_from_db(self, credentials):
        self.credentials_type = credentials.get('type')
        if self.credentials_type == CredentialsTypeChoice.TOKEN:
                self.session_token = credentials.get('value')
        else:
            self.key, second = credentials.get('value').split()
            if self.credentials_type == CredentialsTypeChoice.KEY_TOKEN_PAIR:
                self.session_token = second
            else: self.secret_key = second

    def mapping_with_new_values(self, credentials):
        self.credentials_type = credentials.get('credentials_type', self.credentials_type)
        self.key = credentials.get('key', self.key)
        self.secret_key = credentials.get('secret_key', self.secret_key)
        self.session_token = credentials.get('session_token', self.session_token)

    def values(self):
        return [self.key, self.secret_key, self.session_token]