import boto3
import logging
import os
from jira import JIRA
import json
import hashlib
import re
from boto3.dynamodb.conditions import Key
from datetime import datetime, timezone
logger = logging.getLogger()
logger.setLevel(logging.INFO)

wa_client = boto3.client('wellarchitected')
ta_client = boto3.client('support')
ssm_client = boto3.client('ssm')
resource_group_client = boto3.client('resourcegroupstaggingapi')
dynamodb_resource = boto3.resource('dynamodb')

######################################
# Uncomment below for running on AWS Lambda
######################################
# Jira and OpsCenter integration on/off
OPS_CENTER_INTEGRATION = (os.environ['OPS_CENTER_INTEGRATION'] == 'True')
JIRA_INTEGRATION = (os.environ['JIRA_INTEGRATION'] == 'True')

# Worload related resources (based on tag)
TAG_KEY=os.environ['TAG_KEY']
TAG_VALUE=os.environ['TAG_VALUE']

# WA Implementation plan base-URL
WA_WEB_URL='https://docs.aws.amazon.com/wellarchitected/latest/framework/'
WA_WEB_ANCHOR='.html#implementation-guidance'

# Jira related variables
JIRA_URL = os.environ['JIRA_URL']
JIRA_USERNAME = os.environ['JIRA_USERNAME']
JIRA_SECRET_SSM_PARAM = os.environ['JIRA_SECRET_SSM_PARAM']
JIRA_PROJECT_KEY = os.environ['JIRA_PROJECT_KEY']

# DDB
DDB_TABLE = dynamodb_resource.Table(os.environ['DDB_TABLE'])
######################################

# Function to query the dynamodb table
def ddb_query_entries(ticketHeaderKey):
    response = DDB_TABLE.query(
        KeyConditionExpression=Key('ticketHeaderKey').eq(ticketHeaderKey)
    )
    return response['Items']

# Function to add an entry to the dynamodb table
def ddb_put_entry(ticketId, ticketType, creationDate, updateDate, ticketHeaderKey, ticketContentKey, workloadId, lensAlias, questionId, bestPracticeId):
    response = DDB_TABLE.put_item(
       Item={
            'ticketId': ticketId,
            'ticketType': ticketType,
            'creationDate': creationDate,
            'updateDate': updateDate,
            'ticketHeaderKey': ticketHeaderKey,
            'ticketContentKey': ticketContentKey,
            'workloadId': workloadId,
            'lensAlias': lensAlias,
            'questionId': questionId,
            'bestPracticeId': bestPracticeId
        }
    )
    return response

# Function to update an entry in the dynamodb table
def ddb_update_entry(ticketHeaderKey, creationDate, updateDate, ticketContentKey):
    response = DDB_TABLE.update_item(
        Key={
            'ticketHeaderKey': ticketHeaderKey,
            'creationDate': creationDate
        },
        UpdateExpression="set #u=:u, #t=:t",
        ExpressionAttributeValues={
            ':u': updateDate,
            ':t': ticketContentKey
        },
        ExpressionAttributeNames={
            '#u': 'updateDate',
            '#t': 'ticketContentKey'
        },
        ReturnValues="UPDATED_NEW"
    )
    return response

def get_workload_resources():
    resource_arns = []

    paginator = resource_group_client.get_paginator('get_resources')
    response_iterator = paginator.paginate(TagFilters=[
            {
                'Key': TAG_KEY,
                'Values': [
                    TAG_VALUE,
                ]
            },
        ])

    for page in response_iterator:
        for resource in page['ResourceTagMappingList']:
            resource_arns.append(resource['ResourceARN'])
    
    return resource_arns

def get_unselected_choices(answer):
    selected_choices = answer['SelectedChoices']

    all_choices = []
    for choice in answer['Choices']:
        all_choices.append({'choiceId': choice['ChoiceId'], 'title': choice['Title']})
    
    not_applicable_choices = []
    for choice in answer['ChoiceAnswers']:
        if choice['Status'] in ['NOT_APPLICABLE']:
            not_applicable_choices.append(choice['ChoiceId'])

    unselected_choices = [choice for choice in all_choices if choice['choiceId'] not in selected_choices + not_applicable_choices]
    none_of_these_selected = [choice for choice in all_choices if choice['title'] == 'None of these' and choice['choiceId'] in selected_choices]

    if len(none_of_these_selected) == 0:
        return unselected_choices
    else:
        return all_choices

def get_bp_ta_check_ids_list(check_details):
    bp_ta_check_ids_list = []
    for check in check_details['CheckDetails']:
        bp_ta_check_ids_list.append(check['Id'])
    return bp_ta_check_ids_list

def get_ta_check_summary(bp_ta_check_ids_list):
    bp_ta_checks = []
    ta_checks_list = ta_client.describe_trusted_advisor_checks(
        language='en'
    )['checks']

    filtered_ta_checks_list = [d for d in ta_checks_list if 'id' in d and d['id'] in bp_ta_check_ids_list]

    for check in filtered_ta_checks_list:
        ta_urls = [d for d in re.split('href="|" target=', check['description']) if d.startswith('https')]
        bp_ta_checks.append({'id': check['id'], 'name': check['name'], 'taRecommedationUrls': ta_urls, 'metadataOrder': check['metadata']})

    return bp_ta_checks

def add_flaggedresources(bp_ta_checks, workload_resources):
    for check in bp_ta_checks:
        check['flaggedResources'] = []
        check_result = ta_client.describe_trusted_advisor_check_result(
            checkId=check['id'],
            language='en'
        )['result']
        if check_result['status'] in ['warning', 'error']:
            for flagged_resource in check_result['flaggedResources']:
                if flagged_resource['status'] in ['warning', 'error'] and any(x in flagged_resource['metadata'] for x in workload_resources):
                    check['flaggedResources'].append(flagged_resource)

    return(bp_ta_checks)

def flagged_resource_formatter(check_flagged):
    flagged_resources_list = []
    for resource in check_flagged['flaggedResources']:
        i = 0
        flagged_resource = {}
        for metadata in resource['metadata']:
            flagged_resource[check_flagged['metadataOrder'][i]] = metadata
            i+=1
        flagged_resources_list.append(flagged_resource)

    return (flagged_resources_list)

def create_ops_item(answer, choice, bp_ta_checks, WORKLOAD_ID, LENS_ALIAS):
    bp_ta_checks_flagged = [d for d in bp_ta_checks if len(d['flaggedResources']) > 0]

    if len(bp_ta_checks_flagged) > 0:
        for check_flagged in bp_ta_checks_flagged:
            logger.info(f'Processing Best Practice: {choice["choiceId"]}, and Trusted Advisor check: {check_flagged["name"]}')

            imp_guid_web = WA_WEB_URL + choice['choiceId'] + WA_WEB_ANCHOR
            check_flagged['workloadId'] = WORKLOAD_ID
            check_flagged['pillarId'] = answer['PillarId'],
            check_flagged['questionTitle'] = answer['QuestionTitle']
            check_flagged['risk'] = answer['Risk']
            check_flagged['bestPracticeTitle'] = choice['title']
            check_flagged['implementationGuide'] = imp_guid_web
            flagged_resources_list = flagged_resource_formatter(check_flagged)

            ops_item_description = ("*AWS Well-Architected related information:*\nWorkload Id: " + WORKLOAD_ID +
                "\nPillar Id: " + answer['PillarId'] +
                "\nQuestion: " + answer['QuestionTitle'] +
                "\nRisk: " + answer['Risk'] +
                "\nBest Practice: " + choice['title'] +
                "\n\n*AWS Trusted Advisor (TA) related information:*" + 
                "\nTA Check Id: " + check_flagged['id'] +
                "\nTA Check Name: " + check_flagged['name'] +
                "\n\n*Raw data with resources affected:*" + 
                "\nFlagged Resources (" + str(len(check_flagged['flaggedResources'])) + "):\n " + json.dumps(flagged_resources_list, indent = 3) + 
                "\n\n*Useful link for resolution:*" +
                "\nWell-Architected Implementation Guidance links:\n[" + imp_guid_web + "]" +
                "\n\nTrusted Advisor useful links:\n" + json.dumps(check_flagged['taRecommedationUrls'], indent = 3)
            )

            operation_data = []
            for resource in flagged_resources_list:
                if resource['Resource']:
                    operation_data.append({'arn': resource['Resource']})

            operational_data_object = {
                '/aws/resources': {
                    'Value': json.dumps(operation_data),
                    'Type': 'SearchableString'
                },
                'WorkloadId': {
                    'Value': WORKLOAD_ID,
                    'Type': 'SearchableString'
                },
                'BestPracticeId': {
                    'Value': choice['choiceId'],
                    'Type': 'SearchableString'
                },
                'Runbook': {
                    'Value': imp_guid_web,
                    'Type': 'SearchableString'
                }
            }

            ticketHeaderKey = hashlib.md5(('opscenter' + check_flagged['workloadId'] + check_flagged['bestPracticeTitle'] + check_flagged['id']).encode()).hexdigest()
            ticketContentKey = hashlib.md5(str(check_flagged).encode()).hexdigest()

            ddb_query_response = ddb_query_entries(ticketHeaderKey)
            
            if ddb_query_response:
                if ddb_query_response[0]['ticketContentKey'] != ticketContentKey:
                    logger.info(f'Updating OpsItem issue: {ddb_query_response[0]["ticketId"]}')
                    update_ops_item_response = ssm_client.update_ops_item(
                        Description=ops_item_description,
                        OperationalData=operational_data_object,
                        Title='[WALAB] - ' + check_flagged['name'],
                        OpsItemId=ddb_query_response[0]['ticketId']
                    )
                    ddb_update_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'], datetime.now(timezone.utc).isoformat(), ticketContentKey)
                else:
                    logger.info(f'No changes for OpsItem issue: {ddb_query_response[0]["ticketId"]}')
            else:
                logger.info('Creating OpsItem issue')
                create_ops_item_response = ssm_client.create_ops_item(
                    Description=ops_item_description,
                    OperationalData=operational_data_object,
                    Source='wa_labs',
                    Title='[WALAB] - ' + check_flagged['name']
                )
                ddb_put_entry(create_ops_item_response['OpsItemId'], 'opscenter', datetime.now(timezone.utc).isoformat(), '', ticketHeaderKey, ticketContentKey, check_flagged['workloadId'], LENS_ALIAS, answer['QuestionId'], choice['choiceId'])
                logger.info(f'OpsItem issue {create_ops_item_response["OpsItemId"]} created and recorded in DDB')
    else:
        logger.info(f'No flagged resources for this Best Practice {choice["choiceId"]} on any of its Trusted Advisor checks')

def create_jira_issue(jira_client, answer, choice, bp_ta_checks, WORKLOAD_ID, LENS_ALIAS):
    bp_ta_checks_flagged = [d for d in bp_ta_checks if len(d['flaggedResources']) > 0]

    if len(bp_ta_checks_flagged) > 0:
        for check_flagged in bp_ta_checks_flagged:
            logger.info(f'Processing Best Practice: {choice["choiceId"]}, and Trusted Advisor check: {check_flagged["name"]}')

            imp_guid_web = WA_WEB_URL + choice['choiceId'] + WA_WEB_ANCHOR
            check_flagged['workloadId'] = WORKLOAD_ID
            check_flagged['pillarId'] = answer['PillarId']
            check_flagged['questionTitle'] = answer['QuestionTitle']
            check_flagged['risk'] = answer['Risk']
            check_flagged['bestPracticeTitle'] = choice['title']
            check_flagged['implementationGuide'] = imp_guid_web
            flagged_resources_list = json.dumps(flagged_resource_formatter(check_flagged), indent = 3)

            jira_issue_description = ("*AWS Well-Architected related information:*\nWorkload Id: " + WORKLOAD_ID +
                "\nPillar Id: " + answer['PillarId'] +
                "\nQuestion: " + answer['QuestionTitle'] +
                "\nRisk: " + answer['Risk'] +
                "\nBest Practice: " + choice['title'] +
                "\n\n*AWS Trusted Advisor (TA) related information:*" + 
                "\nTA Check Id: " + check_flagged['id'] +
                "\nTA Check Name: " + check_flagged['name'] +
                "\n\n*Raw data with resources affected:*" + 
                "\nFlagged Resources (" + str(len(check_flagged['flaggedResources'])) + "):\n{color:#97a0af} " + flagged_resources_list + "{color}" + 
                "\n\n*Useful link for resolution:*" +
                "\nWell-Architected Implementation Guidance links:\n[" + imp_guid_web + "]" +
                "\n\nTrusted Advisor useful links:\n" + json.dumps(check_flagged['taRecommedationUrls'], indent = 3)
            )
           
            ticketHeaderKey = hashlib.md5(('jira' + check_flagged['workloadId'] + check_flagged['bestPracticeTitle'] + check_flagged['id']).encode()).hexdigest()
            ticketContentKey = hashlib.md5(str(check_flagged).encode()).hexdigest()

            ddb_query_response = ddb_query_entries(ticketHeaderKey)
            
            if ddb_query_response:
                if ddb_query_response[0]['ticketContentKey'] != ticketContentKey:
                    logger.info(f'Updating JIRA issue: {ddb_query_response[0]["ticketId"]}')
                    jira_client.add_comment(ddb_query_response[0]['ticketId'], jira_issue_description)
                    ddb_update_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'], datetime.now(timezone.utc).isoformat(), ticketContentKey)
                else:
                    logger.info(f'No changes for JIRA issue: {ddb_query_response[0]["ticketId"]}')
            else:
                logger.info('Creating JIRA issue')
                jira_create_issue_response = jira_client.create_issue(
                    project=JIRA_PROJECT_KEY,
                    summary='[WALAB] - ' + check_flagged['name'],
                    description=jira_issue_description,
                    issuetype={'name': 'Task'}
                )
                ddb_put_entry(jira_create_issue_response.key, 'jira', datetime.now(timezone.utc).isoformat(), '', ticketHeaderKey, ticketContentKey, check_flagged['workloadId'], LENS_ALIAS, answer['QuestionId'], choice['choiceId'])
                logger.info(f'JIRA issue {jira_create_issue_response.key} created and recorded in DDB')
    else:
        logger.info(f'No flagged resources for this Best Practice {choice["choiceId"]} on any of its Trusted Advisor checks')

def lambda_handler(event, context):
    if not OPS_CENTER_INTEGRATION and not JIRA_INTEGRATION:
        logger.info('No JIRA/OpsCenter integration enabled')
        return

    ######################################
    # Uncomment below for running on AWS Lambda
    ######################################
    WORKLOAD_ID=event['detail']['requestParameters']['WorkloadId']
    LENS_ALIAS=event['detail']['requestParameters']['LensAlias']
    LENS_ARN=event['detail']['responseElements']['LensArn']
    QUESTION_ID=event['detail']['requestParameters']['QuestionId']
    ######################################

    try:
        if JIRA_INTEGRATION:
            get_parameter_response = ssm_client.get_parameter(Name=JIRA_SECRET_SSM_PARAM,WithDecryption=True)
            jira_secret = str(get_parameter_response['Parameter']['Value'])
            jira_options = {'server': JIRA_URL}
            jira_client = JIRA(options=jira_options, basic_auth=(JIRA_USERNAME,jira_secret))

        answer = wa_client.get_answer(
            WorkloadId=WORKLOAD_ID,
            LensAlias=LENS_ALIAS,
            QuestionId=QUESTION_ID
        )['Answer']

        if not answer['IsApplicable']:
            logger.info(f'Question {QUESTION_ID} for Workload {WORKLOAD_ID} was marked as Not Applicable. Exiting.')
            return

        unselected_choices = get_unselected_choices(answer)
        workload_resources = get_workload_resources()
        
        for choice in unselected_choices:
            check_details = wa_client.list_check_details(
                WorkloadId=WORKLOAD_ID,
                LensArn=LENS_ARN,
                PillarId=answer['PillarId'],
                QuestionId=QUESTION_ID,
                ChoiceId=choice['choiceId']
            )

            bp_ta_check_ids_list = get_bp_ta_check_ids_list(check_details)

            bp_ta_checks = get_ta_check_summary(bp_ta_check_ids_list)

            add_flaggedresources(bp_ta_checks, workload_resources)

            if choice['title'] != 'None of these':
                if OPS_CENTER_INTEGRATION:
                    create_ops_item(answer, choice, bp_ta_checks, WORKLOAD_ID, LENS_ALIAS)

                if JIRA_INTEGRATION:
                    create_jira_issue(jira_client, answer, choice, bp_ta_checks, WORKLOAD_ID, LENS_ALIAS)

    except Exception as e:
        logger.error(f"Error encountered. Exception: {e}")
        raise e
