import boto3
import logging
import os
import json
from boto3.dynamodb.conditions import Key
logger = logging.getLogger()
logger.setLevel(logging.INFO)

wa_client = boto3.client('wellarchitected')
dynamodb_resource = boto3.resource('dynamodb')
sns_client = boto3.client('sns')

######################################
# Uncomment below for running on AWS Lambda
######################################
# Jira and OpsCenter integration on/off
OPS_CENTER_INTEGRATION = (os.environ['OPS_CENTER_INTEGRATION'] == 'True')
JIRA_INTEGRATION = (os.environ['JIRA_INTEGRATION'] == 'True')

# DDB
DDB_TABLE = dynamodb_resource.Table(os.environ['DDB_TABLE'])

# Update BP in WA Tool and Create Milestone automatically on/off
AUTO_BP_MILESTONE_UPDATER = (os.environ['AUTO_BP_MILESTONE_UPDATER'] == 'True')

# SNS
TOPIC_WORKLOAD_BP_UPDATE = os.environ['TOPIC_WORKLOAD_BP_UPDATE']

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

def publish_sns_notification(bestPracticeName, workloadName, managementTool, allIssuesResolved, ticketId, pillarId, pillarQuestion):
    if allIssuesResolved:
        logger.info(f'Sending SNS notification in relation to an update of Best Practice {bestPracticeName} from Workload {workloadName}. All {managementTool} tickets related to this BP have been closed.')
        sns_message = '[WALAB Notification]\t\n\t\nYou are receiving this notification in relation to an update of your Well-Architected Tool Workload "' + workloadName + '" and its alignment with the Well-Architected Best Practice "' + bestPracticeName + '".\t\nThe ' + managementTool + ' ticket ' + ticketId + ' has been closed. All ' + managementTool + ' tickets related to the mentioned Best Practice have been closed.\t\nConsider updating the answer for this Best Practice in your Workload from the Well-Architected Tool.\t\n\t\n[AWS Well-Architected Pilar: "' + pillarId + '", Question: "' + pillarQuestion + '", Best Practice: "' + bestPracticeName + '"]'
        publish_response = sns_client.publish(
            TopicArn=TOPIC_WORKLOAD_BP_UPDATE,
            Message=sns_message,
            Subject='[WALAB] Well-Architected Tool - Workload ' + workloadName + ' Update Notification'
        )
    else:
        logger.info(f'Sending SNS notification in relation to an update of Best Practice {bestPracticeName} from Workload {workloadName}. There are remaining {managementTool} tickets still open in relation to this BP.')
        sns_message = '[WALAB Notification]\t\n\t\nYou are receiving this notification in relation to an update of your Well-Architected Tool Workload "' + workloadName + '" and its alignment with the Well-Architected Best Practice "' + bestPracticeName + '".\t\nThe ' + managementTool + ' ticket ' + ticketId + ' has been closed. There are remaining ' + managementTool + ' tickets still open in relation to this Best Practice.\t\n\t\n[AWS Well-Architected Pilar: "' + pillarId + '", Question: "' + pillarQuestion + '", Best Practice: "' + bestPracticeName + '"]'
        publish_response = sns_client.publish(
            TopicArn=TOPIC_WORKLOAD_BP_UPDATE,
            Message=sns_message,
            Subject='[WALAB] Well-Architected Tool - Workload ' + workloadName + ' Update Notification'
        )

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

                    # Only Update BP in WA Tool and Create Milestone automatically if AUTO_BP_MILESTONE_UPDATER is true
                    if AUTO_BP_MILESTONE_UPDATER:
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
                    
                    publish_sns_notification(ddb_query_response[0]["bestPracticeName"], ddb_query_response[0]["workloadName"], 'Jira', True, ddb_query_response[0]["ticketId"], ddb_query_response[0]["pillarId"], ddb_query_response[0]["pillarQuestion"])

                    logger.info(f'Deleting {ticketId} entry from DDB')
                    ticketHeaderKey = ddb_query_response[0]['ticketHeaderKey']
                    delete_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'])

                elif ddb_query_response and ddb_bp_count > 1:
                    publish_sns_notification(ddb_query_response[0]["bestPracticeName"], ddb_query_response[0]["workloadName"], 'Jira', False, ddb_query_response[0]["ticketId"], ddb_query_response[0]["pillarId"], ddb_query_response[0]["pillarQuestion"])

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

                # Only Update BP in WA Tool and Create Milestone automatically if AUTO_BP_MILESTONE_UPDATER is true
                if AUTO_BP_MILESTONE_UPDATER:
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

                publish_sns_notification(ddb_query_response[0]["bestPracticeName"], ddb_query_response[0]["workloadName"], 'OpsCenter', True, ddb_query_response[0]["ticketId"], ddb_query_response[0]["pillarId"], ddb_query_response[0]["pillarQuestion"])

                logger.info(f'Deleting {ticketId} entry from DDB')
                ticketHeaderKey = ddb_query_response[0]['ticketHeaderKey']
                delete_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'])

            elif ddb_query_response and ddb_bp_count > 1:
                publish_sns_notification(ddb_query_response[0]["bestPracticeName"], ddb_query_response[0]["workloadName"], 'OpsCenter', False, ddb_query_response[0]["ticketId"], ddb_query_response[0]["pillarId"], ddb_query_response[0]["pillarQuestion"])

                logger.info(f'There are outstanding OpsCenter issues related to {ddb_query_response[0]["bestPracticeId"]} in Workload {ddb_query_response[0]["workloadId"]}. Leaving Best Practice in "UNSELECTED" status')
                logger.info(f'Deleting {ticketId} entry from DDB')
                ticketHeaderKey = ddb_query_response[0]['ticketHeaderKey']
                delete_entry(ticketHeaderKey, ddb_query_response[0]['creationDate'])

            else:
                logger.info(f'No entry in DDB for OpsCenter issue: {ticketId}')
    
    except Exception as e:
        logger.error(f"Error encountered. Exception: {e}")
        raise e
        
