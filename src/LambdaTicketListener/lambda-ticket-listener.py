import boto3
import logging
import os
import json
from boto3.dynamodb.conditions import Key
logger = logging.getLogger()
logger.setLevel(logging.INFO)

wa_client = boto3.client('wellarchitected')
dynamodb_resource = boto3.resource('dynamodb')

######################################
# Uncomment below for running on AWS Lambda
######################################
# Jira and OpsCenter integration on/off
OPS_CENTER_INTEGRATION = (os.environ['OPS_CENTER_INTEGRATION'] == 'True')
JIRA_INTEGRATION = (os.environ['JIRA_INTEGRATION'] == 'True')

# DDB
DDB_TABLE = dynamodb_resource.Table(os.environ['DDB_TABLE'])
######################################

# Function to query the dynamodb table based on global index 'ticketId-index' or 'bestPracticeId-index'
def ddb_query_entries(indexName, queryKey):
    response = DDB_TABLE.query(
        IndexName=indexName,
        KeyConditionExpression=Key(indexName.split('-')[0]).eq(queryKey)
    )
    return response['Items']

# Function to delete an entry in the dynamodb table
def delete_entry(ticketHeaderKey, creationDate):
    response = DDB_TABLE.delete_item(
        Key={
            'ticketHeaderKey': ticketHeaderKey,
            'creationDate': creationDate
        }
    )
    return response

def get_none_of_these_choice_id(workloadId, lensAlias, questionId):
    answer = wa_client.get_answer(
        WorkloadId=workloadId,
        LensAlias=lensAlias,
        QuestionId=questionId
    )['Answer']

    for choice in answer['Choices']:
        if choice['Title'] == "None of these":
            return choice['ChoiceId']

def create_milestone(workloadId, ticketId):
    create_milestone_response = wa_client.create_milestone(
        WorkloadId=workloadId,
        MilestoneName=ticketId
    )
    return create_milestone_response

def lambda_handler(event, context):
    try:
        if JIRA_INTEGRATION and event['Records']:
            for record in event['Records']:
                ticketId = json.loads(record['Sns']['Message'])['automationData']['ticketId']
                logger.info(f'JIRA issue {ticketId} was marked as resolved')
                ddb_query_response = ddb_query_entries('ticketId-index', ticketId)
                ddb_bp_count = len(ddb_query_entries('bestPracticeId-index', ddb_query_response[0]['bestPracticeId']))

                if ddb_query_response and ddb_bp_count == 1:
                    none_of_these_choice_id = get_none_of_these_choice_id(ddb_query_response[0]['workloadId'], ddb_query_response[0]['lensAlias'], ddb_query_response[0]['questionId'])

                    logger.info(f'Updating Best Practice {ddb_query_response[0]["bestPracticeId"]} from Workload {ddb_query_response[0]["workloadId"]} to "SELECTED" status')
                    update_answer_response = wa_client.update_answer(
                        WorkloadId=ddb_query_response[0]['workloadId'],
                        LensAlias=ddb_query_response[0]['lensAlias'],
                        QuestionId=ddb_query_response[0]['questionId'],
                        ChoiceUpdates={
                            ddb_query_response[0]['bestPracticeId']: {
                                'Status': 'SELECTED'
                            },
                            none_of_these_choice_id: {
                                'Status': 'UNSELECTED'
                            }
                        }
                    )
                    logger.info(f'Creating new milestone for workload {ddb_query_response[0]["workloadId"]}')
                    create_milestone(ddb_query_response[0]["workloadId"], ticketId)

                    logger.info(f'Deleting {ticketId} entry from DDB')
                    ticketHeaderKey = ddb_query_response[0]['ticketHeaderKey']
                    delete_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'])

                elif ddb_query_response and ddb_bp_count > 1:
                    logger.info(f'There are outstanding JIRA issues related to {ddb_query_response[0]["bestPracticeId"]} in Workload {ddb_query_response[0]["workloadId"]}. Leaving Best Practice in "UNSELECTED" status')
                    logger.info(f'Deleting {ticketId} entry from DDB')
                    ticketHeaderKey = ddb_query_response[0]['ticketHeaderKey']
                    delete_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'])

                else:
                    logger.info(f'No entry in DDB for JIRA issue: {ticketId}')
        
        if OPS_CENTER_INTEGRATION and event['detail']:
            ticketId = event['detail']['requestParameters']['opsItemId']
            logger.info(f'OpsCenter issue {ticketId} was marked as resolved')
            ddb_query_response = ddb_query_entries('ticketId-index', ticketId)
            ddb_bp_count = len(ddb_query_entries('bestPracticeId-index', ddb_query_response[0]['bestPracticeId']))

            if ddb_query_response and ddb_bp_count == 1:
                none_of_these_choice_id = get_none_of_these_choice_id(ddb_query_response[0]['workloadId'], ddb_query_response[0]['lensAlias'], ddb_query_response[0]['questionId'])

                logger.info(f'Updating Best Practice {ddb_query_response[0]["bestPracticeId"]} from Workload {ddb_query_response[0]["workloadId"]} to "SELECTED" status')
                update_answer_response = wa_client.update_answer(
                    WorkloadId=ddb_query_response[0]['workloadId'],
                    LensAlias=ddb_query_response[0]['lensAlias'],
                    QuestionId=ddb_query_response[0]['questionId'],
                    ChoiceUpdates={
                        ddb_query_response[0]['bestPracticeId']: {
                            'Status': 'SELECTED'
                        },
                        none_of_these_choice_id: {
                            'Status': 'UNSELECTED'
                        }
                    }
                )
                logger.info(f'Creating new milestone for workload {ddb_query_response[0]["workloadId"]}')
                create_milestone(ddb_query_response[0]["workloadId"], ticketId)

                logger.info(f'Deleting {ticketId} entry from DDB')
                ticketHeaderKey = ddb_query_response[0]['ticketHeaderKey']
                delete_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'])

            elif ddb_query_response and ddb_bp_count > 1:
                logger.info(f'There are outstanding OpsCenter issues related to {ddb_query_response[0]["bestPracticeId"]} in Workload {ddb_query_response[0]["workloadId"]}. Leaving Best Practice in "UNSELECTED" status')
                logger.info(f'Deleting {ticketId} entry from DDB')
                ticketHeaderKey = ddb_query_response[0]['ticketHeaderKey']
                delete_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'])

            else:
                logger.info(f'No entry in DDB for OpsCenter issue: {ticketId}')
    
    except Exception as e:
        logger.error(f"Error encountered. Exception: {e}")
        raise e
        
