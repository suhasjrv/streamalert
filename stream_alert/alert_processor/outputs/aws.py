"""
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from abc import abstractmethod
from collections import OrderedDict
from datetime import datetime
import json
import uuid

import backoff
from botocore.exceptions import ClientError
import boto3

from stream_alert.alert_processor import LOGGER
from stream_alert.alert_processor.helpers import elide_string_middle
from stream_alert.alert_processor.outputs.output_base import (
    OutputDispatcher,
    OutputProperty,
    StreamAlertOutput
)
from stream_alert.shared.backoff_handlers import (
    backoff_handler,
    success_handler,
    giveup_handler
)


class AWSOutput(OutputDispatcher):
    """Subclass to be inherited from for all AWS service outputs"""

    @classmethod
    def format_output_config(cls, service_config, values):
        """Format the output configuration for this AWS service to be written to disk

        AWS services are stored as a dictionary within the config instead of a list so
        we have access to the AWS value (arn/bucket name/etc) for Terraform

        Args:
            service_config (dict): The actual outputs config that has been read in
            values (OrderedDict): Contains all the OutputProperty items for this service

        Returns:
            dict{<string>: <string>}: Updated dictionary of descriptors and
                values for this AWS service needed for the output configuration
            NOTE: S3 requires the bucket name, not an arn, for this value.
                Instead of implementing this differently in subclasses, all AWSOutput
                subclasses should use a generic 'aws_value' to store the value for the
                descriptor used in configuration
        """
        return dict(service_config.get(cls.__service__, {}),
                    **{values['descriptor'].value: values['aws_value'].value})

    @abstractmethod
    def dispatch(self, alert, descriptor):
        """Placeholder for implementation in the subclasses"""


@StreamAlertOutput
class KinesisFirehoseOutput(AWSOutput):
    """High throughput Alert delivery to AWS S3"""
    MAX_RECORD_SIZE = 1000 * 1000
    MAX_BACKOFF_ATTEMPTS = 3

    __service__ = 'aws-firehose'
    __aws_client__ = None

    @classmethod
    def get_user_defined_properties(cls):
        """Properties assigned by the user when configuring a new Firehose output

        Every output should return a dict that contains a 'descriptor' with a description of the
        integration being configured.

        Returns:
            OrderedDict: Contains various OutputProperty items
        """
        return OrderedDict([
            ('descriptor',
             OutputProperty(
                 description='a short and unique descriptor for this Firehose Delivery Stream')),
            ('aws_value',
             OutputProperty(description='the Firehose Delivery Stream name'))
        ])

    def dispatch(self, alert, descriptor):
        """Send alert to a Kinesis Firehose Delivery Stream

        Args:
            alert (Alert): Alert instance which triggered a rule
            descriptor (str): Output descriptor

        Returns:
            bool: True if alert was sent successfully, False otherwise
        """
        @backoff.on_exception(backoff.fibo,
                              ClientError,
                              max_tries=self.MAX_BACKOFF_ATTEMPTS,
                              jitter=backoff.full_jitter,
                              on_backoff=backoff_handler(),
                              on_success=success_handler(),
                              on_giveup=giveup_handler())
        def _firehose_request_wrapper(json_alert, delivery_stream):
            """Make the PutRecord request to Kinesis Firehose with backoff

            Args:
                json_alert (str): The JSON dumped alert body
                delivery_stream (str): The Firehose Delivery Stream to send to

            Returns:
                dict: Firehose response in the format below
                    {'RecordId': 'string'}
            """
            return self.__aws_client__.put_record(DeliveryStreamName=delivery_stream,
                                                  Record={'Data': json_alert})

        if self.__aws_client__ is None:
            self.__aws_client__ = boto3.client('firehose', region_name=self.region)

        json_alert = json.dumps(alert.output_dict(), separators=(',', ':')) + '\n'
        if len(json_alert) > self.MAX_RECORD_SIZE:
            LOGGER.error('Alert too large to send to Firehose: \n%s...', json_alert[0:1000])
            return False

        delivery_stream = self.config[self.__service__][descriptor]
        LOGGER.info('Sending %s to aws-firehose:%s', alert, delivery_stream)

        resp = _firehose_request_wrapper(json_alert, delivery_stream)

        if resp.get('RecordId'):
            LOGGER.info('%s successfully sent to aws-firehose:%s with RecordId:%s',
                        alert, delivery_stream, resp['RecordId'])

        return self._log_status(resp, descriptor)


@StreamAlertOutput
class LambdaOutput(AWSOutput):
    """LambdaOutput handles all alert dispatching to AWS Lambda"""
    __service__ = 'aws-lambda'

    @classmethod
    def get_user_defined_properties(cls):
        """Get properties that must be assigned by the user when configuring a new Lambda
        output.  This should be sensitive or unique information for this use-case that needs
        to come from the user.

        Every output should return a dict that contains a 'descriptor' with a description of the
        integration being configured.

        Sending to Lambda also requires a user provided Lambda function name and optional qualifier
        (if applicable for the user's use case). A fully-qualified AWS ARN is also acceptable for
        this value. This value should not be masked during input and is not a credential requirement
        that needs encrypted.

        Returns:
            OrderedDict: Contains various OutputProperty items
        """
        return OrderedDict([
            ('descriptor',
             OutputProperty(description='a short and unique descriptor for this Lambda function '
                                        'configuration (ie: abbreviated name)')),
            ('aws_value',
             OutputProperty(description='the AWS Lambda function name, with the optional '
                                        'qualifier (aka \'alias\'), to use for this '
                                        'configuration (ie: output_function:qualifier)',
                            input_restrictions={' '})),
        ])

    def dispatch(self, alert, descriptor):
        """Send alert to a Lambda function

        The alert gets dumped to a JSON string to be sent to the Lambda function

        Args:
            alert (Alert): Alert instance which triggered a rule
            descriptor (str): Output descriptor

        Returns:
            bool: True if alert was sent successfully, False otherwise
        """
        alert_string = json.dumps(alert.record, separators=(',', ':'))
        function_name = self.config[self.__service__][descriptor]

        # Check to see if there is an optional qualifier included here
        # Acceptable values for the output configuration are the full ARN,
        # a function name followed by a qualifier, or just a function name:
        #   'arn:aws:lambda:aws-region:acct-id:function:function-name:prod'
        #   'function-name:prod'
        #   'function-name'
        # Checking the length of the list for 2 or 8 should account for all
        # times a qualifier is provided.
        parts = function_name.split(':')
        if len(parts) == 2 or len(parts) == 8:
            function = parts[-2]
            qualifier = parts[-1]
        else:
            function = parts[-1]
            qualifier = None

        LOGGER.debug('Sending alert to Lambda function %s', function_name)

        client = boto3.client('lambda', region_name=self.region)
        # Use the qualifier if it's available. Passing an empty qualifier in
        # with `Qualifier=''` or `Qualifier=None` does not work and thus we
        # have to perform different calls to client.invoke().
        if qualifier:
            resp = client.invoke(FunctionName=function,
                                 InvocationType='Event',
                                 Payload=alert_string,
                                 Qualifier=qualifier)
        else:
            resp = client.invoke(FunctionName=function,
                                 InvocationType='Event',
                                 Payload=alert_string)

        return self._log_status(resp, descriptor)


@StreamAlertOutput
class S3Output(AWSOutput):
    """S3Output handles all alert dispatching for AWS S3"""
    __service__ = 'aws-s3'

    @classmethod
    def get_user_defined_properties(cls):
        """Get properties that must be assigned by the user when configuring a new S3
        output.  This should be sensitive or unique information for this use-case that needs
        to come from the user.

        Every output should return a dict that contains a 'descriptor' with a description of the
        integration being configured.

        S3 also requires a user provided bucket name to be used for this service output. This
        value should not be masked during input and is not a credential requirement
        that needs encrypted.

        Returns:
            OrderedDict: Contains various OutputProperty items
        """
        return OrderedDict([
            ('descriptor',
             OutputProperty(
                 description='a short and unique descriptor for this S3 bucket (ie: bucket name)')),
            ('aws_value',
             OutputProperty(description='the AWS S3 bucket name to use for this S3 configuration'))
        ])

    def dispatch(self, alert, descriptor):
        """Send alert to an S3 bucket

        Organizes alert into the following folder structure:
            service/entity/rule_name/datetime.json
        The alert gets dumped to a JSON string

        Args:
            alert (Alert): Alert instance which triggered a rule
            descriptor (str): Output descriptor

        Returns:
            bool: True if alert was sent successfully, False otherwise
        """
        bucket = self.config[self.__service__][descriptor]

        # Prefix with alerts to account for generic non-streamalert buckets
        # Produces the following key format:
        #   alerts/dt=2017-01-25-00/kinesis_my-stream_my-rule_uuid.json
        # Keys need to be unique to avoid object overwriting
        key = 'alerts/dt={}/{}_{}_{}_{}.json'.format(
            datetime.now().strftime('%Y-%m-%d-%H'),
            alert.source_service,
            alert.source_entity,
            alert.rule_name,
            uuid.uuid4()
        )

        LOGGER.debug('Sending %s to S3 bucket %s with key %s', alert, bucket, key)

        client = boto3.client('s3', region_name=self.region)
        resp = client.put_object(Body=json.dumps(alert.output_dict()), Bucket=bucket, Key=key)

        return self._log_status(resp, descriptor)


@StreamAlertOutput
class SNSOutput(AWSOutput):
    """Handle all alert dispatching for AWS SNS"""
    __service__ = 'aws-sns'

    @classmethod
    def get_user_defined_properties(cls):
        """Properties assigned by the user when configuring a new SNS output.

        Returns:
            OrderedDict: With 'descriptor' and 'aws_value' OutputProperty tuples
        """
        return OrderedDict([
            ('descriptor', OutputProperty(
                description='a short and unique descriptor for this SNS topic')),
            ('aws_value', OutputProperty(description='SNS topic name'))
        ])

    def dispatch(self, alert, descriptor):
        """Send alert to an SNS topic

        Args:
            alert (Alert): Alert instance which triggered a rule
            descriptor (str): Output descriptor

        Returns:
            bool: True if alert was sent successfully, False otherwise
        """
        # SNS topics can only be accessed via their ARN
        topic_name = self.config[self.__service__][descriptor]
        topic_arn = 'arn:aws:sns:{}:{}:{}'.format(self.region, self.account_id, topic_name)
        topic = boto3.resource('sns', region_name=self.region).Topic(topic_arn)

        response = topic.publish(
            Message=json.dumps(alert.output_dict(), indent=2, sort_keys=True),
            # Subject must be < 100 characters long
            Subject=elide_string_middle(
                '{} triggered alert {}'.format(alert.rule_name, alert.alert_id), 99)
        )
        return self._log_status(response, descriptor)


@StreamAlertOutput
class SQSOutput(AWSOutput):
    """Handle all alert dispatching for AWS SQS"""
    __service__ = 'aws-sqs'

    @classmethod
    def get_user_defined_properties(cls):
        """Properties assigned by the user when configuring a new SQS output.

        Returns:
            OrderedDict: With 'descriptor' and 'aws_value' OutputProperty tuples
        """
        return OrderedDict([
            ('descriptor', OutputProperty(
                description='a short and unique descriptor for this SQS queue')),
            ('aws_value', OutputProperty(description='SQS queue name'))
        ])

    def dispatch(self, alert, descriptor):
        """Send alert to an SQS queue

        Args:
            alert (Alert): Alert instance which triggered a rule
            descriptor (str): Output descriptor

        Returns:
            bool: True if alert was sent successfully, False otherwise
        """
        # SQS queues can only be accessed via their URL
        queue_name = self.config[self.__service__][descriptor]
        queue_url = 'https://sqs.{}.amazonaws.com/{}/{}'.format(
            self.region, self.account_id, queue_name)
        queue = boto3.resource('sqs', region_name=self.region).Queue(queue_url)

        response = queue.send_message(MessageBody=json.dumps(alert.output_dict()))
        return self._log_status(response, descriptor)
