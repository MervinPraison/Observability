from datetime import datetime
import json
import os
from typing import Dict, List, Any, Tuple, Literal

import pandas as pd
import tomli
from tqdm import tqdm

from .data_generator.scenario_generator import ScenarioGenerator, ScenarioInput
from .data_generator.test_case_generator import TestCaseGenerator, TestCaseInput
from .evaluator import Evaluator, EvaluationInput, Conversation
from .utils.issue_description import get_issue_description
from .upload_result import UploadResult

class RedTeaming:
    def __init__(
        self,
        model_name: Literal["gpt-4-1106-preview", "grok-2-latest"] = "grok-2-latest",
        provider: Literal["openai", "xai"] = "xai",
        api_key: str = 'your_api_key',  
        scenario_temperature: float = 0.7,
        test_temperature: float = 0.8,
        eval_temperature: float = 0.3,
    ):
        """
        Initialize the red teaming pipeline.
        
        Args:
            model_name: The OpenAI model to use
            scenario_temperature: Temperature for scenario generation
            api_key: Api Key for the provider
            test_temperature: Temperature for test case generation
            eval_temperature: Temperature for evaluation (lower for consistency)
        """
        # Load supported detectors configuration
        self._load_supported_detectors()
        
        # Initialize generators and evaluator
        self.scenario_generator = ScenarioGenerator(model_name=model_name, temperature=scenario_temperature, provider=provider, api_key=api_key)
        self.test_generator = TestCaseGenerator(model_name=model_name, temperature=test_temperature, provider=provider, api_key=api_key)
        self.evaluator = Evaluator(model_name=model_name, temperature=eval_temperature, provider=provider, api_key=api_key)

        self.save_path = None

    def upload_result(self, project_name, dataset_name):
        upload_result = UploadResult(project_name)
        if self.save_path is None:
            print('Please execute the RedTeaming run() method before uploading the result')
            return
        upload_result.upload_result(csv_path=self.save_path, dataset_name=dataset_name)

        
    def _load_supported_detectors(self) -> None:
        """Load supported detectors from TOML configuration file."""
        config_path = os.path.join(os.path.dirname(__file__), "config", "detectors.toml")
        try:
            with open(config_path, "rb") as f:
                config = tomli.load(f)
                self.supported_detectors = set(config.get("detectors", {}).get("detector_names", []))
        except FileNotFoundError:
            print(f"Warning: Detectors configuration file not found at {config_path}")
            self.supported_detectors = set()
        except Exception as e:
            print(f"Error loading detectors configuration: {e}")
            self.supported_detectors = set()
    
    def validate_detectors(self, detectors: List[str]) -> None:
        """Validate that all provided detectors are supported.
        
        Args:
            detectors: List of detector IDs to validate
            
        Raises:
            ValueError: If any detector is not supported
        """
        unsupported = [d for d in detectors if d not in self.supported_detectors]
        if unsupported:
            raise ValueError(
                f"Unsupported detectors: {unsupported}\n"
                f"Supported detectors are: {sorted(self.supported_detectors)}"
            )
        
    def get_supported_detectors(self) -> List[str]:
        """Get the list of supported detectors."""
        return sorted(self.supported_detectors)
    
    def _get_save_path(self, description: str) -> str:
        """Generate a path for saving the final DataFrame."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(os.path.dirname(__file__), "results")
        os.makedirs(output_dir, exist_ok=True)
        
        # Create a short slug from the description
        slug = description.lower()[:30].replace(" ", "_")
        return os.path.join(output_dir, f"red_teaming_{slug}_{timestamp}.csv")

    def _save_results_to_csv(self, result_df: pd.DataFrame, description: str) -> str:
        # Save DataFrame
        save_path = self._get_save_path(description)
        result_df.to_csv(save_path, index=False)
        print(f"\nResults saved to: {save_path}")
        return save_path

    def _run_with_examples(self, description: str, detectors: List[str], response_model: Any, examples: List[str], scenarios_per_detector: int) -> pd.DataFrame:
        # take care of total no of scenarios limit
        MAX_TOTAL_SCENARIOS = 5
        if len(detectors) >= MAX_TOTAL_SCENARIOS:
            scenarios_per_detector = 1
        elif len(detectors) * scenarios_per_detector >= MAX_TOTAL_SCENARIOS:
            k = 1
            while len(detectors) * k <= MAX_TOTAL_SCENARIOS:
                k += 1
            scenarios_per_detector = k - 1

        # generate the scenarios
        scenarios = []
        for detector in tqdm(detectors, desc=f"Generating scenarios"):
            if type(detector) == str:
            # Get issue description for this detector
                issue_description = get_issue_description(detector)
            else:
                issue_description = detector.get("custom", "")
            
            # Generate scenarios for this detector
            scenario_input = ScenarioInput(
                description=description,
                category=issue_description,
                scenarios_per_detector=scenarios_per_detector
            )
            scenario = self.scenario_generator.generate_scenarios(scenario_input)
            scenarios.extend(scenario)

        # Evaluate the examples against the scenarios
        results = []
        failed_tests = 0
        print('-'*100)
        for example in tqdm(examples, desc=f"Evaluating examples"):
                user_message = example
                app_response = response_model(user_message)
                
                # Evaluate the conversation
                eval_input = EvaluationInput(
                    description=description,
                    conversation=Conversation(
                        user_message=user_message,
                        app_response=app_response
                    ),
                    scenarios=scenarios
                )
                evaluation = self.evaluator.evaluate_conversation(eval_input)
                
                # Store results
                results.append({
                    "detector": detectors,
                    "scenario": scenarios,
                    "user_message": user_message,
                    "app_response": app_response,
                    "evaluation_score": "pass" if evaluation["eval_passed"] else "fail",
                    "evaluation_reason": evaluation["reason"]
                })
                
                if not evaluation["eval_passed"]:
                    failed_tests += 1
        
        # Report results
        total_examples = len(examples)
        if failed_tests > 0:
            print(f"{failed_tests}/{total_examples} tests failed")
        else:
            print(f"All {total_examples} tests passed")
        print('-'*250)

        # Save results to a CSV file
        results_df = pd.DataFrame(results)
        save_path = self._save_results_to_csv(results_df, description)
        self.save_path = save_path

        return results_df, save_path

    
    def _run_without_examples(self, description: str, detectors: List[str], response_model: Any, model_input_format: Dict[str, Any], scenarios_per_detector: int, test_cases_per_scenario: int) -> pd.DataFrame:
        results = []
        # Process each detector
        for detector in detectors:
            print('='*50)
            print(f"Running detector: {detector}")
            print('='*50)

            if type(detector) == str:
            # Get issue description for this detector
                issue_description = get_issue_description(detector)
            else:
                issue_description = detector.get("custom", "")
            
            # Generate scenarios for this detector
            scenario_input = ScenarioInput(
                description=description,
                category=issue_description,
                scenarios_per_detector=scenarios_per_detector
            )
            scenarios = self.scenario_generator.generate_scenarios(scenario_input)
            
            # Process each scenario
            for r, scenario in enumerate(scenarios):
                # Generate test cases
                test_input = TestCaseInput(
                    description=description,
                    category=issue_description,
                    scenario=scenario,
                    format_example=model_input_format,
                    languages=["English"],
                    num_inputs=test_cases_per_scenario
                )
                test_cases = self.test_generator.generate_test_cases(test_input)
                
                # Evaluate test cases
                failed_tests = 0
                with tqdm(test_cases["inputs"],
                         desc=f"Evaluating {detector} scenario {r+1}/{len(scenarios)}") as pbar:
                    for test_case in pbar:
                        user_message = test_case["user_input"]
                        app_response = response_model(user_message)
                        
                        # Evaluate the conversation
                        eval_input = EvaluationInput(
                            description=description,
                            conversation=Conversation(
                                user_message=user_message,
                                app_response=app_response
                            ),
                            scenarios=[scenario]
                        )
                        evaluation = self.evaluator.evaluate_conversation(eval_input)
                        
                        # Store results
                        results.append({
                            "detector": detector,
                            "scenario": scenario,
                            "user_message": user_message,
                            "app_response": app_response,
                            "evaluation_score": "pass" if evaluation["eval_passed"] else "fail",
                            "evaluation_reason": evaluation["reason"]
                        })
                        
                        if not evaluation["eval_passed"]:
                            failed_tests += 1
                
                # Report results for this scenario
                total_tests = len(test_cases["inputs"])
                if failed_tests > 0:
                    print(f"{detector} scenario {r+1}: {failed_tests}/{total_tests} tests failed")
                else:
                    print(f"{detector} scenario {r+1}: All {total_tests} tests passed")
                print('-'*100)

        # Save results to a CSV file
        results_df = pd.DataFrame(results)
        save_path = self._save_results_to_csv(results_df, description)
        self.save_path = save_path

        return results_df, save_path

        
    def run(
        self,
        description: str,
        detectors: List[str],
        response_model: Any,
        examples: List[str] = [],
        model_input_format: Dict[str, Any] = {
            "user_input": "Hi, I am looking for job recommendations",
            "user_name": "John"
        },
        scenarios_per_detector: int = 4,
        test_cases_per_scenario: int = 5 # used only if examples are not provided
    ) -> pd.DataFrame:
        """
        Run the complete red teaming pipeline.
        
        Args:
            description: Description of the app being tested
            detectors: List of detector names to test against (e.g., ["stereotypes", "harmful_content"])
            response_model: Function that takes a user message and returns the app's response
            model_input_format: Format for test case generation
            examples: List of example inputs to test. If provided, uses these instead of generating test cases
            scenarios_per_detector: Number of test scenarios to generate per detector
            test_cases_per_scenario: Number of test cases to generate per scenario
            
        Returns:
            DataFrame containing all test results with columns:
            - scenario: The scenario being tested
            - user_message: The test input
            - app_response: The model's response
            - evaluation_score: Score of whether the response passed evaluation
            - evaluation_reason: Reason for pass/fail
        """

        # Validate detectors
        inbuild_detector = []
        for detector in detectors:
            if type(detector) == str:
                inbuild_detector.append(detector)
            elif type(detector) == dict:
                if 'custom' not in detector.keys() or len(detector.keys()) != 1:
                    raise('The custom detector must be a dictionary with only key "custom" and a string as a value')
            else:
                raise('Detector must be a string or a dictionary with only key "custom" and a string as a value')

        self.validate_detectors(inbuild_detector)
        
        if examples:
            return self._run_with_examples(description, detectors, response_model, examples, scenarios_per_detector)
            
        return self._run_without_examples(description, detectors, response_model, model_input_format, scenarios_per_detector, test_cases_per_scenario)
