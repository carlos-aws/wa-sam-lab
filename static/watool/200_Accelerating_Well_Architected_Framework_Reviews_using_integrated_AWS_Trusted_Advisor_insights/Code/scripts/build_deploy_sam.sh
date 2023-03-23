#!/bin/bash

# Build Script
JiraURL=$1
JiraUsername=$2
JiraProjectKey=$3
EmailAddress=$4

sudo yum install jq -y -q
AWS_REGION=$(curl --silent http://169.254.169.254/latest/dynamic/instance-identity/document | jq '.region' | sed -e 's/^"//' -e 's/"$//')

read -s -p "Enter your Jira API Token: " JIRA_API_TOKEN
echo -e '\n##############################'
echo 'Creating System Manager Parameter Store Secret with Jira API Token'
echo '##############################'
aws ssm put-parameter --name walabjirasecret --type SecureString --value $JIRA_API_TOKEN

cd ~/environment/aws-well-architected-labs/static/watool/200_Accelerating_Well_Architected_Framework_Reviews_using_integrated_AWS_Trusted_Advisor_insights/Code/SAM/

echo -e '\n##############################'
echo 'SAM Build Step'
echo '##############################'
sam build

echo -e '\n##############################'
echo 'SAM Deploy Step'
echo '##############################'
sam deploy --resolve-s3 --no-confirm-changeset --stack-name well-architected-tool-ta-jira-lab-sam --capabilities CAPABILITY_IAM --region $AWS_REGION --parameter-overrides ParameterKey=OpsCenterIntegration,ParameterValue=False ParameterKey=JiraIntegration,ParameterValue=True ParameterKey=WorkloadTagKey,ParameterValue=ApplicationID ParameterKey=WorkloadTagValue,ParameterValue=MySampleWorkload ParameterKey=JiraURL,ParameterValue=$1 ParameterKey=JiraUsername,ParameterValue=$2 ParameterKey=JiraSecretSSMParam,ParameterValue=walabjirasecret ParameterKey=JiraProjectKey,ParameterValue=$3 ParameterKey=WorkloadAccountRoleName,ParameterValue=WAToolTrustedRole ParameterKey=ScanAll,ParameterValue=False ParameterKey=AutoBpMilestoneUpdater,ParameterValue=False ParameterKey=EmailAddress,ParameterValue=$4
